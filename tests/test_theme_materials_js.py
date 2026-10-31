"""Shared chrome materials cover every surface without changing the tracing window."""

import ast
import json
from pathlib import Path

import pytest
from node_host import run_node

STATIC = Path(__file__).resolve().parents[1] / "nd2wsi/static"

# These are display surfaces, not image/annotation fills or semantic accent colours.
SURFACES = {
    "style.css": {
        "bar": ["#toolbar", "#statusbar", "#time-line", "#plate-strip"],
        "panel": [".mac-window"],
        "popover": ["#ann-editor", "#plate-view-menu", "#ann-recovery", "#save-status-tooltip"],
        "hud": ["#pixel-hud", "#landmark-hint", "#zoom-cluster", "#plate-transpose",
                ".site .pill", "#z-label", "#plate-back", ".scale-opt .tag"],
        "toast": ["#toast"],
    },
    "native-v1.css": {
        "bar": ["#toolbar", "#statusbar"],
        "panel": [".mac-window"],
        "popover": ["#ann-editor"],
        "hud": ["#zoom-cluster"],
    },
    "native-shell-v1.css": {
        "bar": ["#tabbar", "#compare-controls"],
        "popover": ["#shading-progress", "#shading-confirm", "#compare-picker", ".window-actions"],
        "toast": ["#shell-toast"],
    },
    "update-card-v1.css": {"popover": ["#update-notice"]},
}

SETUP = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const read = name => fs.readFileSync(path.join(process.argv[1], name), 'utf8').replace(/\r\n/g, '\n');
const spec = JSON.parse(process.argv[2]);
function splitList(text) {
  const parts = [];
  let depth = 0, quote = null, start = 0;
  for (let index = 0; index < text.length; index++) {
    const char = text[index];
    if (quote) { if (char === quote && text[index - 1] !== '\\') quote = null; continue; }
    if (char === '"' || char === "'") quote = char;
    else if (char === '(' || char === '[') depth++;
    else if (char === ')' || char === ']') depth--;
    else if (char === ',' && !depth) { parts.push(text.slice(start, index).trim()); start = index + 1; }
  }
  return [...parts, text.slice(start).trim()];
}
// Retain at-rule conditions so no-blur fallbacks and reduced-motion overrides
// are evaluated separately from the normal material, without a CSS package.
function rules(text, conditions = []) {
  text = text.replace(/\/\*[\s\S]*?\*\//g, '');
  const result = [];
  let offset = 0;
  while (offset < text.length) {
    const opening = text.indexOf('{', offset);
    if (opening < 0) break;
    const header = text.slice(offset, opening).trim();
    let closing = opening + 1, depth = 1, quote = null;
    for (; closing < text.length && depth; closing++) {
      const char = text[closing];
      if (quote) { if (char === quote && text[closing - 1] !== '\\') quote = null; continue; }
      if (char === '"' || char === "'") quote = char;
      else if (char === '{') depth++;
      else if (char === '}') depth--;
    }
    assert.equal(depth, 0, `Unclosed CSS rule: ${header}`);
    const body = text.slice(opening + 1, closing - 1);
    if (header.startsWith('@')) result.push(...rules(body, [...conditions, header]));
    else result.push({
      selectors: splitList(header), conditions,
      declarations: Object.fromEntries(body.split(';').filter(s => s.includes(':')).map(s => {
        const colon = s.indexOf(':');
        return [s.slice(0, colon).trim(), s.slice(colon + 1).trim()];
      })),
    });
    offset = closing;
  }
  return result;
}
function applicable(rule, {backdrop = true, motion = 'no-preference', contrast = 'no-preference', width = 1280} = {}) {
  return rule.conditions.every(condition => {
    if (/^@(?:-webkit-)?keyframes/.test(condition)) return false;
    if (/^@supports/.test(condition) && condition.includes('backdrop-filter')) {
      return /^@supports\s+not\b/.test(condition) ? !backdrop : backdrop;
    }
    const preference = condition.match(/prefers-reduced-motion:\s*(reduce|no-preference)/);
    if (preference && preference[1] !== motion) return false;
    const contrastPreference = condition.match(/prefers-contrast:\s*(more|no-preference)/);
    if (contrastPreference && contrastPreference[1] !== contrast) return false;
    const maxWidth = condition.match(/max-width:\s*(\d+)px/);
    return !maxWidth || width <= Number(maxWidth[1]);
  });
}
function declarations(sheet, selector, environment) {
  const found = rules(sheet).filter(rule => rule.selectors.includes(selector) && applicable(rule, environment));
  assert.ok(found.length, `Missing selector: ${selector}`);
  return Object.assign({}, ...found.map(rule => rule.declarations));
}
function compareRank(a, b) {
  for (let index = 0; index < a.length; index++) if (a[index] !== b[index]) return a[index] - b[index];
  return 0;
}
function specificity(selector) {
  const score = [0, 0, 0];
  // :is/:not/:has contribute their most specific argument; :where contributes
  // nothing. The production state selectors have no nested selector functions.
  selector = selector.replace(/:(is|not|has|where)\(([^()]*)\)/g, (_, kind, contents) => {
    if (kind !== 'where') {
      const highest = splitList(contents).map(specificity).sort(compareRank).at(-1);
      highest.forEach((value, index) => { score[index] += value; });
    }
    return '';
  });
  assert.ok(!/:(is|not|has|where)\(/.test(selector), `Unsupported nested selector: ${selector}`);
  selector = selector.replace(/\[[^\]]*\]/g, () => { score[1]++; return ''; });
  selector = selector.replace(/#[\w-]+/g, () => { score[0]++; return ''; });
  selector = selector.replace(/\.[\w-]+/g, () => { score[1]++; return ''; });
  selector = selector.replace(/::[\w-]+/g, () => { score[2]++; return ''; });
  selector = selector.replace(/:[\w-]+(?:\([^)]*\))?/g, () => { score[1]++; return ''; });
  score[2] += (selector.match(/[a-zA-Z][\w-]*/g) || []).length;
  return score;
}
// Callers list the selectors that match a concrete element/state. Compare
// author !important, specificity, then source order; a late low-specificity
// rule must not hide the light-theme switch bug. Conditions are selected by
// callers when @media rules matter, rather than pretending this is a browser.
function cascade(sheet, matchingSelectors, environment) {
  const matches = new Set(matchingSelectors), winners = {};
  rules(sheet).forEach((rule, order) => {
    if (!applicable(rule, environment)) return;
    for (const selector of rule.selectors.filter(selector => matches.has(selector))) {
      for (const [property, raw] of Object.entries(rule.declarations)) {
        const important = /\s*!important\s*$/.test(raw);
        const rank = [Number(important), ...specificity(selector), order];
        if (!winners[property] || compareRank(rank, winners[property].rank) >= 0) {
          winners[property] = {value: raw.replace(/\s*!important\s*$/, ''), rank};
        }
      }
    }
  });
  return Object.fromEntries(Object.entries(winners).map(([property, winner]) => [property, winner.value]));
}
function themedDeclarations(sheet, selector, light) {
  declarations(sheet, selector); // fail clearly if the requested base surface disappeared
  return cascade(sheet, [selector, ...(light ? [':root.light ' + selector] : [])]);
}
function controlStyle(css, bases, state, light) {
  const suffixes = ['', ':not(:disabled)'];
  if (state === 'hover' || state === 'active') suffixes.push(':hover', ':hover:not(:disabled)');
  if (state === 'active') suffixes.push(':active', ':active:not(:disabled)');
  const selectors = bases.flatMap(base => suffixes.map(suffix => base + suffix));
  return cascade(css, [...selectors, ...(light ? selectors.map(selector => ':root.light ' + selector) : [])]);
}
function assertSurface(css, selector, kind, context) {
  const style = declarations(css, selector);
  assert.equal(style.background, `var(--material-${kind})`, context);
  assert.equal(style['backdrop-filter'], `var(--material-${kind}-filter)`, context);
  assert.equal(style['-webkit-backdrop-filter'], style['backdrop-filter'], context);
  if (kind !== 'bar') assert.equal(style['box-shadow'], `var(--material-${kind}-shadow)`, context);
  // A light-specific literal must not override the shared material at higher specificity.
  for (const rule of rules(css)) {
    if (rule.selectors.includes(':root.light ' + selector) && rule.declarations.background) {
      assert.equal(rule.declarations.background, style.background, context);
    }
  }
}
function assertMaterialsLoaded(html, prefix, next, context) {
  const material = html.indexOf(`href="${prefix}materials-v1.css"`);
  assert.ok(material >= 0 && material < html.indexOf(`href="${prefix}${next}"`), context);
}
function theme(light, environment) {
  const sheet = read('materials-v1.css');
  return cascade(sheet, [':root', ...(light ? [':root.light'] : [])], environment);
}
function resolve(value, tokens, seen = []) {
  return value.replace(/var\(\s*(--[\w-]+)\s*\)/g, (_, name) => {
    assert.ok(name in tokens, `Undefined token: ${name}`);
    assert.ok(!seen.includes(name), `Cyclic token: ${[...seen, name].join(' -> ')}`);
    return resolve(tokens[name], tokens, [...seen, name]);
  });
}
function rgba(value) {
  const match = value.match(/^rgba\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*\)$/);
  assert.ok(match, `Expected an RGBA colour: ${value}`);
  return match.slice(1).map(Number);
}
function over(foreground, background) {
  return background.map((channel, index) => foreground[index] * foreground[3] + channel * (1 - foreground[3]));
}
function paintedBackgrounds(background, underlying) {
  let variants = [underlying];
  for (const layer of splitList(background).reverse()) {
    const colours = layer.startsWith('linear-gradient(')
      ? [...layer.matchAll(/rgba\([^()]+\)/g)].map(match => rgba(match[0])) : [rgba(layer)];
    assert.ok(colours.length, `A material gradient needs neutral colour stops: ${layer}`);
    for (const colour of colours) assert.ok(colour[0] === colour[1] && colour[1] === colour[2], layer);
    variants = variants.flatMap(base => colours.map(colour => over(colour, base)));
  }
  return variants;
}
function luminance(rgb) {
  const linear = rgb.map(channel => {
    const c = channel / 255;
    return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  });
  return linear[0] * 0.2126 + linear[1] * 0.7152 + linear[2] * 0.0722;
}
function contrastRatio(first, second) {
  const levels = [luminance(first), luminance(second)].sort((a, b) => a - b);
  return (levels[1] + 0.05) / (levels[0] + 0.05);
}
"""


def run(script, spec=None):
    result = run_node(SETUP + script, STATIC, json.dumps(spec))
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("light,canvas,rgb", [(False, "#191919", [25, 25, 25]),
                                              (True, "#FFFFFF", [255, 255, 255])])
def test_base_colours_and_materials_resolve_in_both_themes(light, canvas, rgb):
    run(r"""
const tokens = theme(spec.light);
assert.equal(tokens['--canvas'], spec.canvas);
assert.deepEqual(tokens['--canvas-rgb'].split(',').map(Number), spec.rgb);
assert.equal(tokens['color-scheme'], spec.light ? 'light' : 'dark');
for (const kind of ['bar', 'panel', 'popover', 'hud', 'toast']) {
  const name = '--material-' + kind;
  const bottomLayer = splitList(tokens[name]).at(-1);
  assert.match(bottomLayer, /^rgba\(var\(--canvas-rgb\),\s*0\.\d+\)$/,
               `${name} must keep a translucent base derived from the canvas`);
  const base = rgba(resolve(bottomLayer, tokens));
  assert.deepEqual(base.slice(0, 3), spec.rgb);
  assert.ok(base[3] >= 0.7 && base[3] < 0.95, `${name} must balance translucency and readable chrome`);
  const background = resolve(tokens[name], tokens);
  if (kind === 'panel' || kind === 'popover') assert.ok(background.startsWith('linear-gradient('), name);
  for (const underlying of [[0, 0, 0], [255, 255, 255]]) {
    for (const painted of paintedBackgrounds(background, underlying)) {
      for (const label of ['--label', '--label-2']) {
        const ratio = contrastRatio(over(rgba(resolve(tokens[label], tokens)), painted), painted);
        assert.ok(ratio >= 4.5, `${name} ${label} contrast ${ratio.toFixed(2)} over ${underlying}`);
      }
    }
  }
  assert.match(resolve(tokens[name + '-filter'], tokens), /^blur\(\d+px\) saturate\(\d+%\)$/);
}
for (const [name, value] of Object.entries(tokens)) {
  const resolved = resolve(value, tokens);
  assert.ok(!resolved.includes('var('), `${name} has an unresolved custom property`);
}
assert.match(resolve(tokens['--hairline'], tokens), /^rgba\(/);
assert.notEqual(theme(false)['--material-panel-shadow'], theme(true)['--material-panel-shadow']);
""", {"light": light, "canvas": canvas, "rgb": rgb})


@pytest.mark.parametrize("light", [False, True])
def test_hosts_without_backdrop_blur_keep_an_opaque_readable_fallback_in_each_theme(light):
    run(r"""
const normal = theme(spec.light), fallback = theme(spec.light, {backdrop: false});
for (const kind of ['panel', 'popover']) {
  const name = '--material-' + kind;
  const background = resolve(fallback[name], fallback), colour = rgba(background);
  assert.deepEqual(colour.slice(0, 3), fallback['--canvas-rgb'].split(',').map(Number));
  assert.ok(colour[3] >= 0.95 && colour[3] < 1, `${name} fallback needs enough opacity without blur`);
  assert.notEqual(fallback[name], normal[name], `${name} fallback did not win in the actual theme cascade`);
  for (const underlying of [[0, 0, 0], [255, 255, 255]]) {
    const painted = over(colour, underlying);
    const ink = over(rgba(resolve(fallback['--label-2'], fallback)), painted);
    assert.ok(contrastRatio(ink, painted) >= 4.5, `${name} fallback text contrast`);
  }
}
""", {"light": light})


@pytest.mark.parametrize("sheet", SURFACES)
def test_listed_surfaces_use_their_material_family(sheet):
    run(r"""
const css = read(spec.sheet);
for (const [kind, selectors] of Object.entries(spec.surfaces)) {
  for (const selector of selectors) {
    assertSurface(css, selector, kind, `${spec.sheet}: ${selector}`);
  }
}
""", {"sheet": sheet, "surfaces": SURFACES[sheet]})


def test_native_and_focus_overrides_preserve_surface_materials():
    run(r"""
const pane = read('style.css') + '\n' + read('native-v1.css');
for (const light of [false, true]) {
  const toast = themedDeclarations(pane, '#toast', light);
  assert.equal(toast.background, 'var(--material-toast)');
  assert.equal(toast['box-shadow'], 'var(--material-toast-shadow)');
  assert.equal(toast['backdrop-filter'], 'var(--material-toast-filter)');
  const focused = {...themedDeclarations(pane, '#plate-transpose', light),
                   ...themedDeclarations(pane, '#plate-transpose:focus-visible', light)};
  assert.equal(focused.background, 'var(--material-hud)');
  assert.match(focused['box-shadow'], /^var\(--material-hud-shadow\),/);
  assert.ok(focused['box-shadow'].includes('var(--accent)'), 'The keyboard focus ring must remain visible');
}
""")


def test_nested_controls_use_theme_tokens_and_keep_interaction_states_distinct():
    run(r"""
const pane = read('style.css') + '\n' + read('native-v1.css');
const shell = read('native-shell-v1.css');
for (const [css, selectors] of [[pane, ['.btn', 'input.dim', '#ann-text', '.astromap-note', '.seg']],
                               [shell, ['.compare-picker-current', '.compare-chip', '#shading-cancel']]]) {
  for (const selector of selectors) {
    const fills = [false, true].map(light => {
      const background = themedDeclarations(css, selector, light).background;
      assert.match(background, /^var\(--material(?:-[\w-]+)?\)$/, selector);
      return resolve(background, theme(light));
    });
    assert.notEqual(fills[0], fills[1], `${selector} did not follow the theme`);
  }
}
for (const light of [false, true]) {
  const fill = (css, selector) => resolve(themedDeclarations(css, selector, light).background, theme(light));
  assert.notEqual(fill(pane, '.tb-btn:hover'), fill(pane, '.tb-btn.active'));
  assert.notEqual(fill(shell, '.tab:hover'), fill(shell, '.tab.active'));
  assert.notEqual(fill(pane, '#t-auto'), fill(pane, '#t-auto:hover:not(:disabled)'));
}
""")


def test_light_switch_on_wins_by_specificity_over_the_light_off_fill():
    run(r"""
const css = read('style.css') + '\n' + read('native-v1.css');
assert.deepEqual(specificity(':root.light .switch'), [0, 3, 0]);
assert.deepEqual(specificity('.switch.on'), [0, 2, 0]);
for (const light of [false, true]) {
  const offSelectors = ['button', '.switch', ...(light ? [':root.light .switch'] : [])];
  const onSelectors = [...offSelectors, '.switch.on', ...(light ? [':root.light .switch.on'] : [])];
  const off = cascade(css, offSelectors), on = cascade(css, onSelectors);
  assert.equal(on.background, 'var(--accent)', 'An enabled switch must stay blue in both themes');
  assert.notEqual(resolve(on.background, theme(light)), resolve(off.background, theme(light)));
}
// Reproduce the former bug in memory: placing .switch.on later cannot beat
// the light off rule. This ensures the test does not merely trust source order.
const selectors = [':root.light .switch', '.switch.on'];
assert.equal(cascade(':root.light .switch { background: gray; } .switch.on { background: blue; }',
  selectors).background, 'gray');
""")


def test_primary_buttons_share_fill_ink_edge_and_interaction_tokens_in_panes_cards_and_compare_controls():
    run(r"""
const pane = read('style.css') + '\n' + read('native-v1.css');
const card = read('native-shell-v1.css') + '\n' + read('update-card-v1.css');
const shell = read('native-shell-v1.css');
for (const light of [false, true]) {
  const tokens = theme(light), fills = [];
  for (const [state, token] of [['default', '--primary-fill'], ['hover', '--primary-hover'], ['active', '--primary-pressed']]) {
    const controls = [controlStyle(pane, ['button', '.btn', '.btn.primary'], state, light),
      controlStyle(card, ['button', '#update-notice button', '#update-notice .update-primary'], state, light),
      controlStyle(shell, ['button', '.compare-control', '.compare-control.text-control', '.compare-control.primary'], state, light)];
    for (const style of controls) {
      assert.equal(style.background, `var(${token})`, `${state}: primary fill lost the cascade`);
      assert.equal(style.color, 'var(--primary-ink)');
      assert.ok((style['border-color'] || style.border).endsWith('var(--primary-edge)'));
      assert.equal(style['box-shadow'], state === 'active' ? 'none' : 'var(--control-shadow)');
      assert.ok(!style.filter || style.filter === 'none', 'A brightness filter must not alter primary contrast');
    }
    // Compare controls keep their compact toolbar size; pane/card buttons use
    // the same shared geometry as each other.
    for (const style of controls.slice(0, 2)) {
      assert.equal(style['border-radius'], 'var(--control-radius)');
      assert.equal(style['min-height'], 'var(--control-height)');
    }
    const background = resolve(`var(${token})`, tokens);
    fills.push(background);
    const rgb = value => value.match(/^#([\da-f]{2})([\da-f]{2})([\da-f]{2})$/i).slice(1).map(part => parseInt(part, 16));
    assert.ok(contrastRatio(rgb(background), rgb(resolve('var(--primary-ink)', tokens))) >= 4.5,
      `${state}: primary label must remain readable`);
  }
  assert.equal(new Set(fills).size, 3, 'Default, hover and pressed must stay distinguishable');
}
""")


def test_neutral_button_text_stays_readable_in_all_states_over_panel_and_popover_materials():
    run(r"""
const pane = read('style.css') + '\n' + read('native-v1.css');
const card = read('native-shell-v1.css') + '\n' + read('update-card-v1.css');
for (const light of [false, true]) {
  const tokens = theme(light);
  for (const [state, token] of [['default', '--material-button'], ['hover', '--material-button-hover'],
                               ['active', '--material-button-pressed']]) {
    for (const [host, css, selectors] of [['pane', pane, ['button', '.btn']],
                                        ['card', card, ['button', '#update-notice button']]]) {
      const style = controlStyle(css, selectors, state, light);
      // Cancel is deliberately uncoloured at rest. Its hover/pressed feedback
      // shares the pane tokens, and its text inherits the card's label colour.
      assert.equal(style.background, host === 'card' && state === 'default' ? 'transparent' : `var(${token})`);
      const fill = style.background === 'transparent' ? [0, 0, 0, 0] : rgba(resolve(style.background, tokens));
      const colour = style.color === 'inherit' ? themedDeclarations(css, '#update-notice', light).color : style.color;
      assert.equal(colour, 'var(--label)');
      const ink = rgba(resolve(colour, tokens));
      for (const family of ['panel', 'popover']) {
        const material = resolve(tokens[`--material-${family}`], tokens);
        for (const underlying of [[0, 0, 0], [255, 255, 255]]) {
          for (const surface of paintedBackgrounds(material, underlying)) {
            const background = over(fill, surface);
            const ratio = contrastRatio(over(ink, background), background);
            assert.ok(ratio >= 4.5,
              `${host} ${state} ${light ? 'light' : 'dark'} ${family} contrast ${ratio.toFixed(3)} over ${underlying}`);
          }
        }
      }
    }
  }
}
""")


def test_toolbar_tabs_and_source_badges_share_restrained_control_geometry():
    run(r"""
const pane = read('style.css') + '\n' + read('native-v1.css');
const shell = read('native-shell-v1.css');
for (const light of [false, true]) {
  const tokens = theme(light);
  assert.equal(tokens['--control-height'], '28px');
  assert.equal(tokens['--control-height-large'], '30px');
  assert.equal(tokens['--control-radius'], '8px');
  assert.equal(tokens['--control-radius-small'], '6px');
  const toolbar = themedDeclarations(pane, '.tb-btn', light);
  const tab = themedDeclarations(shell, '.tab', light);
  const badge = themedDeclarations(pane, '.app-mark', light);
  assert.equal(toolbar.height, 'var(--control-height)');
  assert.equal(toolbar['border-radius'], 'var(--control-radius)');
  assert.equal(tab.height, 'var(--control-height-large)');
  assert.equal(tab['border-radius'], 'var(--control-radius)');
  assert.equal(badge['border-radius'], 'var(--control-radius-small)');
  assert.equal(badge['box-shadow'], 'none');
  const active = cascade(shell, ['.tab', '.tab.active', ...(light ? [':root.light .tab.active'] : [])]);
  assert.equal(active['box-shadow'], 'none', 'An active tab should not regain a raised rim');
}
""")


def test_panel_borders_are_the_only_sharp_edge_and_material_shadows_only_add_depth():
    run(r"""
const pane = read('style.css') + '\n' + read('native-v1.css');
const card = read('native-shell-v1.css') + '\n' + read('update-card-v1.css');
for (const light of [false, true]) {
  const tokens = theme(light);
  for (const [css, selector] of [[pane, '.mac-window'], [pane, '#ann-editor'],
                               [pane, '#save-status-tooltip'], [card, '#update-notice']]) {
    const style = themedDeclarations(css, selector, light);
    assert.equal(style.border, '1px solid var(--hairline)', selector);
    assert.equal(style['border-radius'], 'var(--panel-radius)', selector);
  }
  for (const family of ['panel', 'popover', 'hud', 'toast']) {
    const shadow = resolve(tokens[`--material-${family}-shadow`], tokens);
    for (const layer of splitList(shadow)) {
      assert.ok(!layer.includes('inset'), `${family}: an inset rim duplicates the border`);
      const lengths = layer.replace(/rgba\([^)]*\)/g, '').trim().split(/\s+/).map(parseFloat);
      assert.ok(lengths.length >= 3 && lengths[2] > 0, `${family}: zero-blur ring duplicates the border`);
      assert.ok(lengths.length < 4 || lengths[3] === 0, `${family}: spread adds a second panel edge`);
    }
  }
}
""")


def test_passive_level_selection_uses_a_quiet_neutral_fill_with_readable_text():
    run(r"""
const css = read('style.css') + '\n' + read('native-v1.css');
for (const light of [false, true]) {
  const tokens = theme(light);
  const selectors = ['.lamp', '.lamp.active', ...(light ? [':root.light .lamp', ':root.light .lamp.active'] : [])];
  const style = cascade(css, selectors);
  assert.equal(style.background, 'var(--level-selected-fill)');
  assert.equal(style.color, 'var(--level-selected-ink)');
  const fill = rgba(resolve(style.background, tokens));
  assert.ok(fill[0] === fill[1] && fill[1] === fill[2] && fill[3] <= 0.2, 'Level is passive status, not a primary action');
  const canvas = tokens['--canvas-rgb'].split(',').map(Number);
  const painted = over(fill, canvas), ink = over(rgba(resolve(style.color, tokens)), painted);
  assert.ok(contrastRatio(ink, painted) >= 4.5);
}
""")


def test_status_popover_hidden_rows_win_over_the_flex_layout():
    run(r"""
const css = read('style.css') + '\n' + read('native-v1.css');
// Hidden optional rows must beat the flex rule in the cascade so idle
// export/cache rows do not leave empty labels or gauges.
const matching = ['#save-status-tooltip .status-detail', '#save-status-tooltip [hidden]'];
assert.equal(cascade(css, matching).display, 'none');
assert.equal(declarations(css, '#save-status-tooltip').background, 'var(--material-popover)');
assert.equal(declarations(css, '.save-status[data-warning="true"]').color, 'var(--save-warn)');
assert.equal(declarations(css, '.save-status[data-warning="true"] .save-check').display, 'none');
assert.equal(declarations(css, '.save-status[data-warning="true"] .save-alert').display, 'block');
""")


def test_status_popover_enters_from_the_trigger_and_respects_reduced_motion():
    run(r"""
const css = read('style.css') + '\n' + read('native-v1.css');
const selectors = ['*', '#save-status-tooltip', '#save-status-tooltip:not([hidden])'];
const normal = cascade(css, selectors, {motion: 'no-preference'});
assert.equal(normal.animation, 'status-popover-open 140ms ease-out');
assert.ok(normal['transform-origin'].includes('var(--status-origin-x,'));
assert.ok(normal['transform-origin'].includes('var(--status-origin-y,'));
const reduced = cascade(css, selectors, {motion: 'reduce'});
assert.equal(reduced.animation, 'none');
assert.equal(reduced.transform, 'none');
const keyframes = rules(css).filter(rule => rule.conditions.some(condition => condition === '@keyframes status-popover-open'));
const from = keyframes.find(rule => rule.selectors.includes('from')).declarations;
const to = keyframes.find(rule => rule.selectors.includes('to')).declarations;
assert.equal(from.opacity, '0');
assert.match(from.transform, /translateY\(var\(--status-enter-y,\s*6px\)\)\s+scale\(\.94\)/);
assert.equal(to.opacity, '1');
assert.equal(to.transform, 'none');
assert.ok(!cascade(css, ['#save-status-tooltip'], {motion: 'no-preference'}).animation,
  'Only a visible popover may start the entry animation');
""")


def test_all_backdrop_surfaces_and_material_references_are_tokenized():
    run(r"""
let count = 0;
for (const name of spec) {
  const css = read(name);
  for (const rule of rules(css)) {
    const d = rule.declarations;
    if (!d['backdrop-filter'] && !d['-webkit-backdrop-filter']) continue;
    const context = `${name}: ${rule.selectors.join(', ')}`;
    assert.match(d.background, /^var\(--material-(bar|panel|popover|hud|toast)\)$/, context);
    assert.match(d['backdrop-filter'], /^var\(--material-(bar|panel|popover|hud|toast)-filter\)$/, context);
    assert.equal(d['-webkit-backdrop-filter'], d['backdrop-filter'], context);
    count++;
  }
  for (const match of css.matchAll(/var\((--material-[\w-]+)\)/g)) {
    for (const light of [false, true]) assert.ok(theme(light)[match[1]], `${name}: ${match[1]}`);
  }
  // There is one source of truth for the base and material definitions.
  for (const rule of rules(css).filter(r => r.selectors.some(s => /^:root(?:\.light)?$/.test(s)))) {
    for (const property of Object.keys(rule.declarations)) {
      assert.ok(property !== '--canvas' && !property.startsWith('--material-'), `${name}: ${property}`);
    }
  }
}
assert.ok(count >= 30, 'The surface inventory unexpectedly lost a material');
""", list(SURFACES))


def test_shell_pane_and_about_blank_landing_load_the_same_materials():
    from nd2wsi.app import BOOT_HTML

    run(r"""
for (const [name, prefix, next] of [['index.html', '../../static/', 'style.css'],
                                  ['shell.html', 'static/', 'native-shell-v1.css']]) {
  assertMaterialsLoaded(read(name), prefix, next, name);
}
assert.ok(!read('translucent.html').includes('materials-v1.css'));
""")
    materials = (STATIC / "materials-v1.css").read_text(encoding="utf-8")
    update_card = (STATIC / "update-card-v1.css").read_text(encoding="utf-8")
    assert materials in BOOT_HTML and update_card in BOOT_HTML
    assert BOOT_HTML.index(materials) < BOOT_HTML.index(update_card)
    assert "background:var(--canvas)" in BOOT_HTML
    # The standalone WKWebView card check must exercise the production materials,
    # even though it assembles its HTML without shell.html's stylesheet links.
    qa = STATIC.parents[1] / "qa/check_native_update_card.py"
    tree = ast.parse(qa.read_text(encoding="utf-8"))
    stylesheet_lists = [
        [item.value for item in node.elts]
        for node in ast.walk(tree)
        if isinstance(node, ast.Tuple)
        and all(isinstance(item, ast.Constant) and isinstance(item.value, str) for item in node.elts)
    ]
    assert ["materials-v1.css", "native-shell-v1.css", "update-card-v1.css"] in stylesheet_lists


# Separate baselines preserve the official top-right lock and lab bottom-left
# lock when the theme changes merge into the lab line. No tracing file is themed.
TRACING_BASELINES = {
    "official": {
        "translucent.html": "501d9fe63d887f78b390d416759231ea3da3756ad135ad597babf68a7d433c5b",
        "translucent-v1.css": "98dfc58b2722be138a6c41cb4807281d4b5f60c361ae07c1c985c3ef81254845",
        "translucent-v1.js": "1ad1334d459fc6d35f2423fca782313195b64764d74b6b8f9d07e1215ec88d04",
        "../translucent_window.py": "f748a4263c927ea3b6eece87dd9f06078e475a2ed0a875418f2a863d8a8f3483",
    },
    "SLACS-module": {
        "translucent.html": "b5c65681de88c19a027d99957084a678205338b862a3d51bc9b84c087864a6a5",
        "translucent-v1.css": "b6ce0a9bb851acdd7168ca65408d1a62e82c218cc3d4add8baf2dd79b7a38511",
        "translucent-v1.js": "0050bba730717537440f18f515efad379c1d94c10c2d7192763c6206c86c027e",
        "../translucent_window.py": "5de3043b181534f425d8a601f4ce16875a4c90256bd0c145a351a94a967436d1",
    },
}


@pytest.mark.parametrize("name", TRACING_BASELINES["official"])
def test_tracing_files_are_unchanged_for_this_edition(name):
    try:
        from nd2wsi.edition import NAME as edition
    except ModuleNotFoundError as exc:
        if exc.name != "nd2wsi.edition":
            raise
        edition = "official"
    # Normalize checkout line endings so Windows checks the same source bytes.
    run(r"""
const hash = require('node:crypto').createHash('sha256').update(read(spec.name)).digest('hex');
assert.equal(hash, spec.hash, `${spec.edition}: ${spec.name} changed`);
""", {"name": name, "edition": edition, "hash": TRACING_BASELINES[edition][name]})
