/* Markdown and maths, rendered without a dependency.

   Chief is four static files served by the same process, no build step and no CDN (REQ-21),
   and a strict reading of that rules out marked/KaTeX/MathJax — a CDN tag is a network
   dependency for a loopback tool, and vendoring KaTeX is 300KB of JavaScript plus a font
   family for text that is usually three lines of prose. So this is a deliberate subset,
   written to cover what a harness and a person actually type into Chief, and to *say so*
   when it meets something it does not understand rather than mangling it.

   Two rules hold the whole file together.

   **Nothing is ever assembled as an HTML string.** Every node is created and its text set
   through `textContent`, which escapes by construction. Artifact bodies, summaries and
   comments all come from outside — a harness reports them — so `innerHTML` here would be a
   script-injection hole in a page that reads other people's output. There is no sanitiser
   because there is nothing to sanitise.

   **What cannot be rendered is shown, not swallowed.** Unparseable maths falls back to its
   own source in a marked span. A reader who sees `$\nabla_\theta J$` knows what it was
   meant to be; a reader who sees nothing does not know anything is missing. */

const SVG_MATHML = "http://www.w3.org/1998/Math/MathML";

const h = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.setAttribute("class", cls);
  if (text != null) node.textContent = text;
  return node;
};

const m = (tag, text) => {
  const node = document.createElementNS(SVG_MATHML, tag);
  if (text != null) node.textContent = text;
  return node;
};

const add = (parent, children) => {
  for (const child of [].concat(children)) if (child) parent.appendChild(child);
  return parent;
};

// ── maths ────────────────────────────────────────────────────────────────────────────────
//
// LaTeX in, MathML out, rendered by the browser's own maths engine. MathML Core is in every
// current browser, which is what makes a dependency-free path possible at all — the work
// here is only the translation, not the typesetting.

/** Single-token commands: a name in, one character out. */
const SYMBOLS = {
  alpha: "α", beta: "β", gamma: "γ", delta: "δ", epsilon: "ε", varepsilon: "ε", zeta: "ζ",
  eta: "η", theta: "θ", vartheta: "ϑ", iota: "ι", kappa: "κ", lambda: "λ", mu: "μ", nu: "ν",
  xi: "ξ", pi: "π", rho: "ρ", sigma: "σ", tau: "τ", upsilon: "υ", phi: "φ", varphi: "ϕ",
  chi: "χ", psi: "ψ", omega: "ω",
  Gamma: "Γ", Delta: "Δ", Theta: "Θ", Lambda: "Λ", Xi: "Ξ", Pi: "Π", Sigma: "Σ",
  Upsilon: "Υ", Phi: "Φ", Psi: "Ψ", Omega: "Ω",
  times: "×", cdot: "⋅", div: "÷", pm: "±", mp: "∓", ast: "∗",
  leq: "≤", le: "≤", geq: "≥", ge: "≥", neq: "≠", ne: "≠", approx: "≈", equiv: "≡",
  sim: "∼", propto: "∝", ll: "≪", gg: "≫",
  in: "∈", notin: "∉", subset: "⊂", subseteq: "⊆", supset: "⊃", supseteq: "⊇",
  cup: "∪", cap: "∩", emptyset: "∅", forall: "∀", exists: "∃", neg: "¬",
  rightarrow: "→", to: "→", leftarrow: "←", leftrightarrow: "↔", Rightarrow: "⇒",
  mapsto: "↦", implies: "⟹",
  infty: "∞", partial: "∂", nabla: "∇", ldots: "…", cdots: "⋯", dots: "…",
  angle: "∠", perp: "⊥", parallel: "∥", degree: "°", prime: "′",
  land: "∧", lor: "∨", oplus: "⊕", otimes: "⊗", star: "⋆", circ: "∘",
  // Conditioning and norms, which is most of what machine-learning notation needs beyond
  // the above — `p(y \mid x)` came up the first time this met a real write-up.
  mid: "∣", vert: "|", Vert: "‖", lVert: "‖", rVert: "‖", "|": "‖",
  langle: "⟨", rangle: "⟩", lfloor: "⌊", rfloor: "⌋", lceil: "⌈", rceil: "⌉",
  wedge: "∧", vee: "∨", setminus: "∖", simeq: "≃", cong: "≅", models: "⊨", vdash: "⊢",
  odot: "⊙", bullet: "∙", dagger: "†", top: "⊤", bot: "⊥",
  hbar: "ℏ", ell: "ℓ", Re: "ℜ", Im: "ℑ", aleph: "ℵ", colon: ":",
};

/** Commands that take limits above and below when displayed. */
const BIG_OPERATORS = {
  sum: "∑", prod: "∏", coprod: "∐", int: "∫", iint: "∬", oint: "∮",
  bigcup: "⋃", bigcap: "⋂", lim: "lim", max: "max", min: "min", argmax: "arg max",
  argmin: "arg min", sup: "sup", inf: "inf",
};

/** Commands rendered as an upright multi-letter name rather than italic variables. */
const FUNCTIONS = [
  "sin", "cos", "tan", "arcsin", "arccos", "arctan", "sinh", "cosh", "tanh",
  "log", "ln", "exp", "det", "dim", "ker", "deg", "gcd", "mod", "Pr",
];

const OPEN = {
  "(": "(", "[": "[", "\\{": "{", "\\lbrace": "{", "\\langle": "⟨", "\\lfloor": "⌊",
  "\\lceil": "⌈", "|": "|", "\\|": "‖", "\\vert": "|", "\\Vert": "‖", ".": "",
};
const CLOSE = {
  ")": ")", "]": "]", "\\}": "}", "\\rbrace": "}", "\\rangle": "⟩", "\\rfloor": "⌋",
  "\\rceil": "⌉", "|": "|", "\\|": "‖", "\\vert": "|", "\\Vert": "‖", ".": "",
};

class MathError extends Error {}

/** Split LaTeX into the pieces the builder walks: commands, groups, and single characters. */
function lex(src) {
  const out = [];
  let i = 0;
  while (i < src.length) {
    const c = src[i];
    if (c === "\\") {
      const name = /^\\([a-zA-Z]+|.)/.exec(src.slice(i));
      if (!name) throw new MathError("dangling backslash");
      out.push({ kind: "cmd", value: name[1] });
      i += name[0].length;
    } else if (/\s/.test(c)) {
      // Kept rather than dropped: `build` ignores them, but \text{if } needs the space it
      // was given, and rebuilding it from tokens is the only place that can know.
      out.push({ kind: "space", value: " " });
      i += 1;
    } else if (/[0-9]/.test(c)) {
      const run = /^[0-9]*\.?[0-9]+/.exec(src.slice(i))[0];
      out.push({ kind: "num", value: run });
      i += run.length;
    } else if (/[A-Za-z]/.test(c)) {
      // One letter at a time: `ab` is a times b in maths, not a two-letter identifier.
      out.push({ kind: "var", value: c });
      i += 1;
    } else {
      out.push({ kind: "char", value: c });
      i += 1;
    }
  }
  return out;
}

/** Build one MathML row from a token stream, stopping at `until`. */
function build(tokens, state, until) {
  const row = m("mrow");
  const parts = [];

  const attach = (node) => {
    // `^` and `_` bind to whatever came immediately before them, so the row is kept as a
    // list until the end rather than appended to as it goes.
    parts.push(node);
  };

  while (state.i < tokens.length) {
    const token = tokens[state.i];
    if (until && token.kind === until.kind && token.value === until.value) break;
    state.i += 1;

    if (token.kind === "space") {
      continue;
    } else if (token.kind === "num") {
      attach(m("mn", token.value));
    } else if (token.kind === "var") {
      attach(m("mi", token.value));
    } else if (token.kind === "char") {
      if (token.value === "{") {
        attach(build(tokens, state, { kind: "char", value: "}" }));
        expect(tokens, state, "}");
      } else if (token.value === "}") {
        throw new MathError("unbalanced brace");
      } else if (token.value === "^" || token.value === "_") {
        const base = parts.pop() || m("mrow");
        const script = argument(tokens, state);
        // A base that already carries the other script becomes a two-script element rather
        // than a script on a script, which is what puts x_i^2 on one stack.
        const existing = base.tagName === "msub" || base.tagName === "msup" ? base : null;
        if (existing && existing.tagName === "msub" && token.value === "^") {
          const [b, sub] = [...existing.childNodes];
          attach(add(m("msubsup"), [b, sub, script]));
        } else if (existing && existing.tagName === "msup" && token.value === "_") {
          const [b, sup] = [...existing.childNodes];
          attach(add(m("msubsup"), [b, script, sup]));
        } else {
          attach(add(m(token.value === "^" ? "msup" : "msub"), [base, script]));
        }
      } else {
        attach(m("mo", token.value));
      }
    } else {
      attach(command(token.value, tokens, state));
    }
  }
  return add(row, parts);
}

function expect(tokens, state, char) {
  while (tokens[state.i] && tokens[state.i].kind === "space") state.i += 1;
  const token = tokens[state.i];
  if (!token || token.kind !== "char" || token.value !== char) {
    throw new MathError(`expected ${char}`);
  }
  state.i += 1;
}

/** One argument: a braced group, or the single token that follows. */
function argument(tokens, state) {
  while (tokens[state.i] && tokens[state.i].kind === "space") state.i += 1;
  const token = tokens[state.i];
  if (!token) throw new MathError("missing argument");
  if (token.kind === "char" && token.value === "{") {
    state.i += 1;
    const group = build(tokens, state, { kind: "char", value: "}" });
    expect(tokens, state, "}");
    return group;
  }
  state.i += 1;
  if (token.kind === "num") return m("mn", token.value);
  if (token.kind === "var") return m("mi", token.value);
  if (token.kind === "cmd") return command(token.value, tokens, state);
  return m("mo", token.value);
}

/** The literal text of the next braced group, for \text and the font commands. */
function textArgument(tokens, state) {
  expect(tokens, state, "{");
  let out = "";
  let depth = 1;
  while (state.i < tokens.length) {
    const token = tokens[state.i];
    if (token.kind === "char" && token.value === "{") depth += 1;
    if (token.kind === "char" && token.value === "}") {
      depth -= 1;
      if (depth === 0) {
        state.i += 1;
        return out;
      }
    }
    out += token.kind === "cmd" ? `\\${token.value}` : token.value;
    state.i += 1;
  }
  throw new MathError("unterminated \\text");
}

function command(name, tokens, state) {
  if (name in SYMBOLS) return m("mo", SYMBOLS[name]);
  if (FUNCTIONS.includes(name)) return m("mi", name);

  if (name === "frac" || name === "dfrac" || name === "tfrac") {
    return add(m("mfrac"), [argument(tokens, state), argument(tokens, state)]);
  }
  if (name === "sqrt") {
    const next = tokens[state.i];
    if (next && next.kind === "char" && next.value === "[") {
      state.i += 1;
      const index = build(tokens, state, { kind: "char", value: "]" });
      expect(tokens, state, "]");
      return add(m("mroot"), [argument(tokens, state), index]);
    }
    return add(m("msqrt"), [argument(tokens, state)]);
  }
  if (name in BIG_OPERATORS) {
    const op = m("mo", BIG_OPERATORS[name]);
    op.setAttribute("stretchy", "false");
    // Limits go under and over when they are written as sub/superscripts on the operator,
    // which is what makes a sum read as a sum rather than as an indexed variable.
    let under = null;
    let over = null;
    while (tokens[state.i] && tokens[state.i].kind === "char" &&
           (tokens[state.i].value === "_" || tokens[state.i].value === "^")) {
      const which = tokens[state.i].value;
      state.i += 1;
      if (which === "_") under = argument(tokens, state);
      else over = argument(tokens, state);
    }
    if (under && over) return add(m("munderover"), [op, under, over]);
    if (under) return add(m("munder"), [op, under]);
    if (over) return add(m("mover"), [op, over]);
    return op;
  }
  if (name === "text" || name === "textrm" || name === "mbox") {
    return m("mtext", textArgument(tokens, state));
  }
  if (name === "mathbb" || name === "mathbf" || name === "mathrm" || name === "mathcal") {
    const node = m("mi", textArgument(tokens, state));
    node.setAttribute("mathvariant", {
      mathbb: "double-struck", mathbf: "bold", mathrm: "normal", mathcal: "script",
    }[name]);
    return node;
  }
  if (name === "hat" || name === "bar" || name === "vec" || name === "tilde" || name === "dot") {
    const accent = { hat: "^", bar: "‾", vec: "→", tilde: "~", dot: "˙" }[name];
    const mark = m("mo", accent);
    mark.setAttribute("stretchy", "false");
    return add(m("mover"), [argument(tokens, state), mark]);
  }
  if (name === "left" || name === "right") {
    const token = tokens[state.i];
    if (!token) throw new MathError(`\\${name} without a delimiter`);
    state.i += 1;
    const raw = token.kind === "cmd" ? `\\${token.value}` : token.value;
    const table = name === "left" ? OPEN : CLOSE;
    if (!(raw in table)) throw new MathError(`unknown delimiter ${raw}`);
    const glyph = table[raw];
    if (!glyph) return null; // \left. and \right. are invisible by definition
    const node = m("mo", glyph);
    node.setAttribute("fence", "true");
    return node;
  }
  if (name === "quad" || name === "qquad") {
    const space = m("mspace");
    space.setAttribute("width", name === "quad" ? "1em" : "2em");
    return space;
  }
  if (name === "," || name === ";" || name === ":" || name === "!" || name === " ") {
    const space = m("mspace");
    space.setAttribute("width", name === "!" ? "-0.15em" : "0.2em");
    return space;
  }
  if (name === "\\") return m("mspace"); // a line break inside maths: not supported, not fatal
  throw new MathError(`unknown command \\${name}`);
}

/** One `$…$` or `$$…$$` as MathML, or its own source if it cannot be translated.

    The fallback is the important half. This understands a subset, and a reader who is shown
    `$\\nabla_\\theta J(\\theta)$` verbatim can still read it — one who is shown a blank, or a
    silently mangled fragment, cannot tell that anything was lost. */
export function renderMath(source, display = false) {
  try {
    const tokens = lex(source);
    const state = { i: 0 };
    const row = build(tokens, state, null);
    if (state.i !== tokens.length) throw new MathError("trailing input");
    const math = m("math");
    math.setAttribute("display", display ? "block" : "inline");
    if (display) math.setAttribute("class", "math-display");
    return add(math, [row]);
  } catch (err) {
    if (!(err instanceof MathError)) throw err;
    const fence = display ? "$$" : "$";
    const raw = h("span", "math-raw", `${fence}${source}${fence}`);
    raw.setAttribute("title", `Not rendered: ${err.message}`);
    return raw;
  }
}

// ── inline markdown ──────────────────────────────────────────────────────────────────────

/** Only schemes that cannot execute. A `javascript:` href in text a harness reported would
    be a script-injection hole reached by clicking. */
const SAFE_HREF = /^(https?:|mailto:|vscode:|file:|\/|#)/i;

const INLINE = [
  // Order matters: code first, so nothing inside backticks is interpreted further.
  { re: /^`([^`]+)`/, make: (mt) => h("code", "md-code", mt[1]) },
  { re: /^\$\$([^$]+)\$\$/, make: (mt) => renderMath(mt[1].trim(), true) },
  // Neither end may sit against whitespace, which is what keeps "it cost $5 and $6" as
  // prose. The same rule KaTeX's auto-render uses, and for the same reason: a dollar sign
  // is a currency symbol far more often than it opens an equation.
  { re: /^\$([^\s$][^$\n]*?[^\s$]|[^\s$])\$/, make: (mt) => renderMath(mt[1].trim(), false) },
  { re: /^\*\*([^*]+)\*\*/, make: (mt) => add(h("strong"), inline(mt[1])) },
  { re: /^__([^_]+)__/, make: (mt) => add(h("strong"), inline(mt[1])) },
  { re: /^~~([^~]+)~~/, make: (mt) => add(h("del"), inline(mt[1])) },
  { re: /^\*([^*\n]+)\*/, make: (mt) => add(h("em"), inline(mt[1])) },
  // Underscore emphasis only between non-word characters, or snake_case_names break up.
  { re: /^_([^_\n]+)_(?![A-Za-z0-9])/, make: (mt) => add(h("em"), inline(mt[1])) },
  { re: /^\[([^\]]*)\]\(([^)\s]+)\)/, make: (mt) => link(mt[2], mt[1] || mt[2], mt[0]) },
  { re: /^<((?:https?|mailto):[^>\s]+)>/, make: (mt) => link(mt[1], mt[1], mt[0]) },
  { re: /^(https?:\/\/[^\s<>()[\]]+)/, make: (mt) => link(mt[1], mt[1], mt[0]) },
];

function link(href, text, source) {
  // A refused scheme falls back to the markdown as written, not to the link text alone: the
  // reader should see that there was a link and where it pointed, rather than a bare word
  // and no sign anything was dropped.
  if (!SAFE_HREF.test(href)) return document.createTextNode(source);
  const a = h("a", "md-link", text);
  a.setAttribute("href", href);
  a.setAttribute("target", "_blank");
  // Without this the opened page gets a handle on this one through `window.opener`.
  a.setAttribute("rel", "noopener noreferrer");
  return a;
}

/** Inline markdown as a list of nodes. Safe on any input: everything becomes text or a
    element built here, never markup parsed out of the string. */
export function inline(text) {
  const out = [];
  let buffer = "";
  let rest = String(text == null ? "" : text);

  const flush = () => {
    if (buffer) out.push(document.createTextNode(buffer));
    buffer = "";
  };

  while (rest) {
    if (rest[0] === "\\" && rest.length > 1 && /[\\`*_{}[\]()#+\-.!$~]/.test(rest[1])) {
      buffer += rest[1];
      rest = rest.slice(2);
      continue;
    }
    const hit = INLINE.find((rule) => rule.re.test(rest));
    if (hit) {
      const match = hit.re.exec(rest);
      flush();
      out.push(hit.make(match));
      rest = rest.slice(match[0].length);
      continue;
    }
    buffer += rest[0];
    rest = rest.slice(1);
  }
  flush();
  return out;
}

// ── block markdown ───────────────────────────────────────────────────────────────────────

const BULLET = /^[ \t]*[-*+][ \t]+(.*)$/;
const NUMBER = /^[ \t]*(\d+)[.)][ \t]+(.*)$/;
const HEADING = /^(#{1,6})[ \t]+(.*)$/;
const QUOTE = /^>[ \t]?(.*)$/;
const RULE = /^([-*_])(?:[ \t]*\1){2,}[ \t]*$/;

/** Markdown as a list of block nodes.

    A deliberate divergence from CommonMark: a single newline inside a paragraph becomes a
    line break rather than a space. CommonMark reflows them, which is right for prose written
    in a text editor and wrong for everything here — a harness reporting a summary and a
    person typing in a textarea both mean the newline they typed. GitHub made the same choice
    for comments, for the same reason. */
export function markdown(text) {
  const lines = String(text == null ? "" : text).replace(/\r\n?/g, "\n").split("\n");
  const blocks = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];

    if (!line.trim()) {
      i += 1;
      continue;
    }

    // Fenced code: taken verbatim, including anything that looks like markup.
    const fence = /^[ \t]*(```|~~~)(.*)$/.exec(line);
    if (fence) {
      const body = [];
      i += 1;
      while (i < lines.length && !new RegExp(`^[ \\t]*${fence[1]}`).test(lines[i])) {
        body.push(lines[i]);
        i += 1;
      }
      i += 1; // the closing fence, or the end of the text
      const pre = h("pre", "md-pre");
      const code = h("code", null, body.join("\n"));
      if (fence[2].trim()) code.setAttribute("data-lang", fence[2].trim());
      blocks.push(add(pre, [code]));
      continue;
    }

    // Display maths on its own lines, which is how it is usually written.
    const open = /^[ \t]*\$\$(.*)$/.exec(line);
    if (open) {
      const body = [];
      if (open[1].trim().endsWith("$$")) {
        body.push(open[1].trim().slice(0, -2));
        i += 1;
      } else {
        if (open[1].trim()) body.push(open[1]);
        i += 1;
        while (i < lines.length && !/\$\$/.test(lines[i])) {
          body.push(lines[i]);
          i += 1;
        }
        if (i < lines.length) {
          body.push(lines[i].replace(/\$\$.*$/, ""));
          i += 1;
        }
      }
      blocks.push(add(h("div", "md-math"), [renderMath(body.join(" ").trim(), true)]));
      continue;
    }

    if (RULE.test(line)) {
      blocks.push(h("hr", "md-hr"));
      i += 1;
      continue;
    }

    const heading = HEADING.exec(line);
    if (heading) {
      const level = Math.min(6, heading[1].length);
      blocks.push(add(h(`h${level + 2 > 6 ? 6 : level + 2}`, "md-h"), inline(heading[2])));
      i += 1;
      continue;
    }

    if (QUOTE.test(line)) {
      const body = [];
      while (i < lines.length && QUOTE.test(lines[i])) {
        body.push(QUOTE.exec(lines[i])[1]);
        i += 1;
      }
      blocks.push(add(h("blockquote", "md-quote"), markdown(body.join("\n"))));
      continue;
    }

    if (BULLET.test(line) || NUMBER.test(line)) {
      const ordered = !BULLET.test(line) && NUMBER.test(line);
      const list = h(ordered ? "ol" : "ul", "md-list");
      while (i < lines.length && (BULLET.test(lines[i]) || NUMBER.test(lines[i]))) {
        const item = BULLET.exec(lines[i]) || NUMBER.exec(lines[i]);
        const content = BULLET.test(lines[i]) ? item[1] : item[2];
        const li = add(h("li"), inline(content));
        // Wrapped lines belong to the item above them, not to a new paragraph.
        i += 1;
        while (
          i < lines.length && lines[i].trim() &&
          !BULLET.test(lines[i]) && !NUMBER.test(lines[i]) && !HEADING.test(lines[i]) &&
          !QUOTE.test(lines[i])
        ) {
          add(li, [document.createTextNode(" "), ...inline(lines[i].trim())]);
          i += 1;
        }
        add(list, [li]);
      }
      blocks.push(list);
      continue;
    }

    // A paragraph: everything up to a blank line or the start of another block.
    const para = [];
    while (
      i < lines.length && lines[i].trim() &&
      !HEADING.test(lines[i]) && !QUOTE.test(lines[i]) && !RULE.test(lines[i]) &&
      !BULLET.test(lines[i]) && !NUMBER.test(lines[i]) &&
      !/^[ \t]*(```|~~~)/.test(lines[i]) && !/^[ \t]*\$\$/.test(lines[i])
    ) {
      para.push(lines[i]);
      i += 1;
    }
    const p = h("p", "md-p");
    para.forEach((text, index) => {
      if (index) p.appendChild(h("br"));
      add(p, inline(text));
    });
    blocks.push(p);
  }

  return blocks;
}

/** Markdown into an element, replacing whatever was there. Convenience for the callers that
    build a container and want it filled. */
export function fill(node, text) {
  node.replaceChildren(...markdown(text));
  return node;
}
