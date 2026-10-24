"""Auto-save status tracker: channel precedence, texts, countdown and ticker."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1] / "nd2wsi/static/save-status-v1.js"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

HARNESS = r"""
const {createTracker}=require(process.argv[1]);
let now=100000,nextId=1;const intervals=new Map(),renders=[];
const tracker=createTracker({now:()=>now,onRender:status=>renders.push(status),
  setInterval:(fn,ms)=>{const id=nextId++;intervals.set(id,{fn,ms});return id},
  clearInterval:id=>intervals.delete(id)});
const tick=(seconds=1)=>{for(let i=0;i<seconds;i++){now+=1000;for(const {fn} of [...intervals.values()])fn()}};
const brief=status=>[status.state,status.text,status.visible];
"""


def run(body):
    result = subprocess.run(
        [NODE, "-e", HARNESS + body, str(MODULE)],
        capture_output=True, encoding="utf-8", check=True, timeout=20,
    )
    return json.loads(result.stdout)


def test_idle_until_the_first_event_then_precedence_across_channels():
    out = run("""
const steps=[];
steps.push(brief(tracker.describe(now)));
tracker.report('annotations',{state:'saved',at:now});steps.push(brief(tracker.describe(now)));
tracker.report('lut',{state:'dirty'});steps.push(brief(tracker.describe(now)));
tracker.report('annotations',{state:'saving'});steps.push(brief(tracker.describe(now)));
tracker.report('lut',{state:'failed',message:'HTTP 503'});steps.push(brief(tracker.describe(now)));
tracker.report('annotations',{state:'conflict',conflict:true,message:'Conflict'});steps.push(brief(tracker.describe(now)));
tracker.report('annotations',{state:'saved'});steps.push(brief(tracker.describe(now)));
tracker.report('lut',{state:'saved'});steps.push(brief(tracker.describe(now)));
console.log(JSON.stringify(steps));
""")
    assert out == [
        ["idle", "", False],
        ["saved", "Auto-saved 0 s ago", True],
        ["dirty", "Unsaved changes…", True],
        ["saving", "Saving…", True],
        ["failed", "Not saved\nHTTP 503", True],
        ["conflict", "Not saved · conflict, see Annotations", True],
        ["failed", "Not saved\nHTTP 503", True],
        ["saved", "Auto-saved 0 s ago", True],
    ]


def test_saved_text_counts_seconds_minutes_and_hours_from_the_latest_save():
    out = run("""
tracker.report('lut',{state:'saved',at:now-30000});
tracker.report('annotations',{state:'saved',at:now});
const at=seconds=>tracker.describe(now+seconds*1000).text;
console.log(JSON.stringify([at(0),at(1),at(59),at(60),at(119),at(3599),at(3600),at(7199),at(90000),
  tracker.describe(now-5000).text]));
""")
    assert out == [
        "Auto-saved 0 s ago", "Auto-saved 1 s ago", "Auto-saved 59 s ago",
        "Auto-saved 1 min ago", "Auto-saved 1 min ago", "Auto-saved 59 min ago",
        "Auto-saved 1 h ago", "Auto-saved 1 h ago", "Auto-saved 25 h ago",
        "Auto-saved 0 s ago",
    ]


def test_latest_saved_time_survives_a_later_idle_report_and_is_shared_across_channels():
    out = run("""
tracker.report('annotations',{state:'saved',at:now-20000});
tracker.report('lut',{state:'saved',at:now-5000});
tracker.report('lut',{state:'idle'});
const afterIdle=tracker.describe(now).text;
tracker.report('annotations',{state:'dirty'});
tracker.report('annotations',{state:'saved',at:now});
console.log(JSON.stringify([afterIdle,tracker.describe(now+2000).text]));
""")
    assert out == ["Auto-saved 5 s ago", "Auto-saved 2 s ago"]


def test_failed_text_counts_down_to_the_scheduled_retry_with_the_error_after_a_line_break():
    out = run("""
tracker.report('annotations',{state:'failed',message:'HTTP 500'});
tracker.scheduleRetry(60000);
const texts=[tracker.describe(now).text,tracker.describe(now+1000).text,tracker.describe(now+59500).text,
  tracker.describe(now+60000).text,tracker.describe(now+61000).text];
tracker.setRetryAt(null);
console.log(JSON.stringify({texts,none:tracker.describe(now).text,retryAt:tracker.describe(now).retryAt}));
""")
    assert out["texts"] == [
        "Not saved · retrying in 60 s\nHTTP 500", "Not saved · retrying in 59 s\nHTTP 500",
        "Not saved · retrying in 1 s\nHTTP 500", "Not saved · retrying in 0 s\nHTTP 500",
        "Not saved · retrying in 0 s\nHTTP 500",
    ]
    assert out["none"] == "Not saved\nHTTP 500" and out["retryAt"] is None


def test_unavailable_channel_is_ignored_for_state_but_keeps_its_message_in_details():
    out = run("""
tracker.report('lut',{state:'unavailable',message:'LUT settings are not saved for this source.'});
const hidden=tracker.describe(now);
tracker.report('annotations',{state:'saved',at:now});
const saved=tracker.describe(now+3000);
tracker.report('annotations',{state:'failed',message:'offline'});
console.log(JSON.stringify({hidden:brief(hidden),hiddenDetails:hidden.details,saved:brief(saved),
  details:saved.details,failed:brief(tracker.describe(now))}));
""")
    assert out["hidden"] == ["idle", "", False]
    assert out["hiddenDetails"] == "LUT settings are not saved for this source."
    assert out["saved"] == ["saved", "Auto-saved 3 s ago", True]
    assert out["details"] == "LUT settings are not saved for this source."
    assert out["failed"] == ["failed", "Not saved\noffline", True]


def test_a_dirty_channel_with_a_message_keeps_it_in_details_for_the_tooltip():
    out = run("""
tracker.report('lut',{state:'dirty',message:"LUT values are invalid for this image's pixel type."});
const invalid=tracker.describe(now);
tracker.report('annotations',{state:'saved',at:now});
const withSaved=tracker.describe(now);
tracker.report('lut',{state:'dirty'});
const plain=tracker.describe(now);
tracker.report('lut',{state:'failed',message:'HTTP 503'});
const failed=tracker.describe(now);
const full=status=>[...brief(status),status.details];
console.log(JSON.stringify({invalid:full(invalid),withSaved:full(withSaved),plain:full(plain),failed:full(failed)}));
""")
    reason = "LUT values are invalid for this image's pixel type."
    assert out["invalid"] == ["dirty", "Unsaved changes…", True, reason]
    assert out["withSaved"] == ["dirty", "Unsaved changes…", True, reason]
    assert out["plain"] == ["dirty", "Unsaved changes…", True, None]
    # a failure's message is part of the text, not a detail
    assert out["failed"] == ["failed", "Not saved\nHTTP 503", True, None]


def test_ticker_renders_only_when_state_or_text_changes_and_reports_render_immediately():
    out = run("""
tracker.start();
const afterStart=renders.length;
tick(5);
const idleTicks=renders.length;
tracker.report('annotations',{state:'dirty'});
const afterDirty=renders.length;
tick(3);
const dirtyTicks=renders.length;
tracker.report('annotations',{state:'saving'});
tracker.report('annotations',{state:'saving'});
const afterSaving=renders.length;
tracker.report('annotations',{state:'saved'});
tick(3);
const texts=renders.slice(afterSaving).map(s=>s.text);
console.log(JSON.stringify({afterStart,idleTicks,afterDirty,dirtyTicks,afterSaving,texts,
  first:renders[0],interval:[...intervals.values()].map(t=>t.ms)}));
""")
    assert out["afterStart"] == 1 and out["first"]["visible"] is False
    assert out["idleTicks"] == 1
    assert out["afterDirty"] == 2 and out["dirtyTicks"] == 2
    assert out["afterSaving"] == 3
    assert out["texts"] == ["Auto-saved 0 s ago", "Auto-saved 1 s ago", "Auto-saved 2 s ago", "Auto-saved 3 s ago"]
    assert out["interval"] == [1000]


def test_dispose_stops_the_interval_and_later_events_render_nothing():
    out = run("""
tracker.start();tracker.start();
const running=[intervals.size,tracker.isRunning()];
tracker.report('lut',{state:'saved'});
tracker.dispose();
const count=renders.length;
tracker.report('lut',{state:'failed',message:'late'});
tracker.start();tick(2);
console.log(JSON.stringify({running,intervals:intervals.size,isRunning:tracker.isRunning(),
  rendersAfter:renders.length-count,current:tracker.current().state}));
""")
    assert out["running"] == [1, True]
    assert out["intervals"] == 0 and out["isRunning"] is False
    assert out["rendersAfter"] == 0 and out["current"] == "saved"


def test_unknown_states_are_rejected():
    out = run("""
let error=null;try{tracker.report('lut',{state:'done'})}catch(e){error=e.message}
console.log(JSON.stringify(error));
""")
    assert out == "unknown save state: done"
