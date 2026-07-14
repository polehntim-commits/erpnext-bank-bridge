// SPDX-License-Identifier: MIT
// ERPNext Bank Bridge — reusable custom autocomplete dropdown (v0.3.3).
//
// A tiny, dependency-free replacement for <datalist> that can render rich rows
// (merchant + txn count + $ total). Extracted from the inline rules-page script
// so the filter logic is unit-testable in Node (see tests/test_rule_dropdown.py)
// and shared by every custom dropdown in the rule builder.
//
// v0.3.2 shipped this widget inline and hid the menu whenever the typed query
// matched nothing (`display:none` on an empty result set), so typing collapsed
// the dropdown and it only reappeared on a fresh focus. This module keeps the
// menu open while the user types, live-filters on every keystroke, shows a
// "use as new" empty state, and adds arrow-key navigation.
//
// UMD-ish: attaches to `window.BankBridgeDropdown` in the browser and exports
// via `module.exports` under Node (the filter fn is pure and touches no DOM at
// require time).
(function (root, factory) {
  var api = factory();
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (root) root.BankBridgeDropdown = api;
})(typeof self !== 'undefined' ? self : (typeof globalThis !== 'undefined' ? globalThis : this), function () {
  'use strict';

  // ── Pure, testable core ──────────────────────────────────────────
  // Case-insensitive substring match of `query` against each option's label.
  // A blank query returns every option (a fresh copy). null/undefined labels
  // are treated as '' so a bad row never throws mid-filter.
  function filterOptions(options, query, getLabel) {
    var opts = options || [];
    var label = getLabel || function (o) { return o == null ? '' : String(o); };
    var q = (query == null ? '' : String(query)).trim().toLowerCase();
    if (!q) return opts.slice();
    return opts.filter(function (o) {
      var s = label(o);
      s = (s == null ? '' : String(s)).toLowerCase();
      return s.indexOf(q) !== -1;
    });
  }

  // ── Browser component ────────────────────────────────────────────
  var STYLE_ID = 'bb-dd-style';
  function injectStyle() {
    if (typeof document === 'undefined') return;
    if (document.getElementById(STYLE_ID)) return;
    var s = document.createElement('style');
    s.id = STYLE_ID;
    s.textContent =
      '.bb-dd-opt{padding:6px 9px;cursor:pointer;border-bottom:1px solid #f0f0f0}' +
      '.bb-dd-opt[data-bb-active="1"]{background:#eef3ff}' +
      '.bb-dd-empty{padding:6px 9px;color:#888;font-size:12px;cursor:default}';
    (document.head || document.documentElement).appendChild(s);
  }

  // createDropdown wires an <input> to a menu <div>. Config:
  //   input      required — the text input
  //   menu       required — the (absolutely positioned) container div
  //   getOptions () => [option]           — full, unfiltered list (called live)
  //   getLabel   (option) => string       — text used for substring matching
  //   renderRow  (option) => htmlString   — inner HTML of one option row
  //   onSelect   (option) => void         — a row was clicked / Enter-selected
  //   onInput    (value)  => void         — optional, after every keystroke
  //   emptyRow   (query)  => htmlString|null — empty-state markup; null hides
  //   enabled    () => bool               — dropdown active? (e.g. merchant mode)
  //   limit      number (default 50)
  // Returns { open, close, refresh, isOpen }.
  function createDropdown(cfg) {
    injectStyle();
    var input = cfg.input, menu = cfg.menu;
    var limit = cfg.limit || 50;
    var getLabel = cfg.getLabel || function (o) { return o == null ? '' : String(o); };
    var enabled = cfg.enabled || function () { return true; };
    var items = [];        // current filtered options (what's on screen)
    var active = -1;       // highlighted row index, -1 = none
    var open = false;      // is a real option list showing?

    function close() {
      open = false; active = -1; menu.style.display = 'none'; menu.innerHTML = '';
    }

    function paintActive() {
      var rows = menu.querySelectorAll('.bb-dd-opt');
      for (var i = 0; i < rows.length; i++) {
        if (i === active) {
          rows[i].setAttribute('data-bb-active', '1');
          if (rows[i].scrollIntoView) rows[i].scrollIntoView({ block: 'nearest' });
        } else {
          rows[i].removeAttribute('data-bb-active');
        }
      }
    }

    function choose(i) {
      var opt = items[i];
      if (opt === undefined) return;
      input.value = getLabel(opt);
      close();
      if (cfg.onSelect) cfg.onSelect(opt);
    }

    // Recompute the filtered list from the live input value and (re)render.
    function render() {
      if (!enabled()) { close(); return; }
      items = filterOptions(cfg.getOptions() || [], input.value, getLabel).slice(0, limit);
      if (items.length) {
        menu.innerHTML = items.map(function (opt, i) {
          return '<div class="bb-dd-opt" data-i="' + i + '">' + cfg.renderRow(opt) + '</div>';
        }).join('');
        active = 0;
        Array.prototype.forEach.call(menu.querySelectorAll('.bb-dd-opt'), function (el) {
          var i = +el.getAttribute('data-i');
          // mousedown (not click) so the input never blurs out from under us.
          el.addEventListener('mousedown', function (ev) { ev.preventDefault(); choose(i); });
          el.addEventListener('mouseenter', function () { active = i; paintActive(); });
        });
        paintActive();
        menu.style.display = 'block';
        open = true;
      } else {
        var empty = cfg.emptyRow ? cfg.emptyRow(input.value) : null;
        active = -1;
        if (empty == null) { close(); return; }
        menu.innerHTML = '<div class="bb-dd-empty">' + empty + '</div>';
        menu.style.display = 'block';
        open = true;               // visible, but nothing selectable
      }
    }

    input.addEventListener('focus', render);
    input.addEventListener('input', function () {
      render();
      if (cfg.onInput) cfg.onInput(input.value);
    });
    input.addEventListener('keydown', function (ev) {
      var k = ev.key;
      if (k === 'ArrowDown' || k === 'ArrowUp') {
        if (!open) { render(); return; }
        if (!items.length) return;
        ev.preventDefault();
        active = (active + (k === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
        paintActive();
      } else if (k === 'Enter') {
        // Only intercept Enter while the menu is open on a real option, so
        // Enter with no matches falls through to "keep what I typed as new".
        if (open && items.length && active >= 0) { ev.preventDefault(); choose(active); }
        else { close(); }
      } else if (k === 'Escape') {
        close();
      }
    });

    // Click outside → close, keeping whatever the user typed. A blur timeout
    // would race the row mousedown; a document listener does not.
    if (typeof document !== 'undefined') {
      document.addEventListener('mousedown', function (ev) {
        if (!open) return;
        if (ev.target === input || menu.contains(ev.target)) return;
        close();
      });
    }

    return {
      open: render,
      close: close,
      refresh: function () { if (open) render(); },
      isOpen: function () { return open; }
    };
  }

  return { filterOptions: filterOptions, createDropdown: createDropdown };
});
