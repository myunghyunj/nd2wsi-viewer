"""Lab shell cards share the public materials without theming the tracing window."""

import pytest
from test_theme_materials_js import run

pytestmark = pytest.mark.slacs_edition


@pytest.mark.parametrize("light", [False, True])
def test_lab_cards_follow_the_selected_theme_and_keep_their_visibility_contract(light):
    run(r"""
const css = read('native-shell-v1.css');
const tokens = theme(spec.light);
for (const selector of ['#slacs-offer', '#translucent-warning']) {
  assertSurface(css, selector, 'popover', selector);
  const style = themedDeclarations(css, selector, spec.light);
  assert.equal(style['border-radius'], 'var(--panel-radius)');
  assert.equal(style.border, '1px solid var(--hairline)');
  assert.equal(style.color, 'var(--label)');
  assert.equal(declarations(css, selector + '[hidden]').display, 'none');
  for (const property of ['background', 'box-shadow', 'backdrop-filter', 'border', 'color']) {
    assert.ok(!resolve(style[property], tokens).includes('var('), `${selector}: ${property}`);
  }
}
// The shell switches themes with :root.light. An OS/data-attribute override
// would leave this warning dark even when the user explicitly selected light.
for (const rule of rules(css).filter(rule => rule.selectors.some(selector => selector.includes('#translucent-warning')))) {
  assert.ok(!rule.selectors.some(selector => selector.includes('[data-theme')));
  assert.ok(!rule.conditions.some(condition => condition.includes('prefers-color-scheme')));
}
""", {"light": light})


@pytest.mark.parametrize("light", [False, True])
def test_lab_actions_keep_distinct_neutral_and_primary_interaction_states(light):
    run(r"""
const css = read('native-shell-v1.css');
const tokens = theme(spec.light);
const neutralBases = ['.slacs-offer-actions button', '#translucent-warning button'];
for (const [state, neutral, primary] of [
  ['default', '--material-button', '--primary-fill'],
  ['hover', '--material-button-hover', '--primary-hover'],
  ['active', '--material-button-pressed', '--primary-pressed'],
]) {
  for (const base of neutralBases) {
    const style = controlStyle(css, ['button', base], state, spec.light);
    assert.equal(style.background, `var(${neutral})`);
    assert.equal(style.color, 'var(--label)');
    assert.equal(style['border-radius'], 'var(--control-radius)');
    assert.equal(style['min-height'], 'var(--control-height-large)');
    assert.equal(style['box-shadow'], state === 'active' ? 'none' : 'var(--control-shadow)');
    assert.ok(!style.filter || style.filter === 'none');
    assert.ok(!resolve(style.background, tokens).includes('var('));
  }
  const style = controlStyle(css, ['button', '.slacs-offer-actions button', '#slacs-offer-open'], state, spec.light);
  assert.equal(style.background, `var(${primary})`, `${state}: the primary action must win the cascade`);
  assert.equal(style.color, 'var(--primary-ink)');
  assert.equal(style['border-color'], 'var(--primary-edge)');
  assert.ok(!style.filter || style.filter === 'none');
}
for (const base of neutralBases) {
  assert.equal(declarations(css, base + ':disabled').opacity, 'var(--control-disabled-opacity)');
  const focus = declarations(css, base + ':focus-visible');
  assert.equal(focus.outline, '2px solid var(--accent)');
  assert.equal(focus['outline-offset'], '3px');
}
""", {"light": light})
