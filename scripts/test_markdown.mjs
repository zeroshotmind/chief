/* Unit tests for src/chief/web/markdown.js, run under node against a stub DOM.

   Separate from smoke_ui.mjs on purpose. That one drives whole screens and answers "does the
   app still render"; this one answers "is the output right for this input", which needs many
   small cases rather than one long walk. Both run without a browser and without a build.

       node scripts/test_markdown.mjs

   The escaping cases are the ones to keep. Everything here comes from outside — a harness
   reports artifact bodies and summaries — so the guarantee that markup in the input becomes
   *text* in the output is a security property, not a formatting nicety. */

import assert from "node:assert";

// ── the smallest DOM these functions need ────────────────────────────────────────────────

function node(tag, ns) {
  const self = {
    tagName: tag, ns, attrs: {}, childNodes: [],
    setAttribute(k, v) { self.attrs[k] = String(v); },
    getAttribute(k) { return self.attrs[k] ?? null; },
    appendChild(c) { self.childNodes.push(c); return c; },
    replaceChildren(...c) { self.childNodes = c; },
    set textContent(v) { self.childNodes = [{ text: String(v) }]; },
    get textContent() { return text(self); },
  };
  return self;
}
globalThis.document = {
  createElement: (t) => node(t, null),
  createElementNS: (ns, t) => node(t, ns),
  createTextNode: (t) => ({ text: String(t) }),
};

const { markdown, inline, renderMath } = await import("../src/chief/web/markdown.js");
const mdx = (src) => markdown(src, { mdx: true });

/** All the text in a subtree, which is what the reader ends up seeing. */
function text(n) {
  if (n.text != null) return n.text;
  return (n.childNodes || []).map(text).join("");
}

/** A crude serialisation, so a test can assert structure without walking by hand. */
function shape(n) {
  if (n.text != null) return JSON.stringify(n.text);
  const cls = n.attrs && n.attrs.class ? `.${n.attrs.class.split(" ").join(".")}` : "";
  const kids = (n.childNodes || []).map(shape).join(",");
  return `${n.tagName}${cls}${kids ? `[${kids}]` : ""}`;
}

const find = (n, tag, out = []) => {
  if (n.tagName === tag) out.push(n);
  for (const c of n.childNodes || []) if (c.tagName) find(c, tag, out);
  return out;
};
const findAll = (nodes, tag) => nodes.flatMap((n) => find(n, tag));

let passed = 0;
const failures = [];
function check(name, fn) {
  try {
    fn();
    passed += 1;
  } catch (err) {
    failures.push(`${name}\n    ${err.message.split("\n").join("\n    ")}`);
  }
}

// ── line breaks ──────────────────────────────────────────────────────────────────────────

check("a single newline inside a paragraph becomes a line break", () => {
  const [p] = markdown("first line\nsecond line");
  assert.equal(p.tagName, "p");
  assert.equal(find(p, "br").length, 1);
  assert.equal(text(p), "first linesecond line");
});

check("a blank line starts a new paragraph", () => {
  const blocks = markdown("one\n\ntwo");
  assert.equal(blocks.length, 2);
  assert.deepEqual(blocks.map((b) => b.tagName), ["p", "p"]);
});

check("trailing and leading blank lines produce no empty blocks", () => {
  assert.equal(markdown("\n\n  \nhello\n\n\n").length, 1);
});

check("empty and null input render nothing rather than throwing", () => {
  assert.deepEqual(markdown(""), []);
  assert.deepEqual(markdown(null), []);
  assert.deepEqual(markdown(undefined), []);
});

// ── blocks ───────────────────────────────────────────────────────────────────────────────

check("headings carry their level", () => {
  const blocks = markdown("# one\n\n## two\n\n### three");
  assert.deepEqual(blocks.map((b) => b.tagName), ["h3", "h4", "h5"]);
  assert.equal(text(blocks[1]), "two");
});

check("a hash without a space is not a heading", () => {
  assert.equal(markdown("#nothashtag").at(0).tagName, "p");
});

check("bullets group into one list", () => {
  const [list] = markdown("- a\n- b\n* c");
  assert.equal(list.tagName, "ul");
  assert.equal(find(list, "li").length, 3);
});

check("numbers group into an ordered list", () => {
  const [list] = markdown("1. first\n2. second");
  assert.equal(list.tagName, "ol");
  assert.equal(text(list), "firstsecond");
});

check("a wrapped list item stays in its item", () => {
  const [list] = markdown("- a line that\n  continues here\n- second");
  const items = find(list, "li");
  assert.equal(items.length, 2);
  assert.equal(text(items[0]), "a line that continues here");
});

check("a blockquote nests its blocks", () => {
  const [quote] = markdown("> quoted **thing**\n> and more");
  assert.equal(quote.tagName, "blockquote");
  assert.equal(find(quote, "strong").length, 1);
});

check("a rule is a rule and a dash list is not", () => {
  assert.equal(markdown("---").at(0).tagName, "hr");
  assert.equal(markdown("- item").at(0).tagName, "ul");
});

check("a fenced block is verbatim, markup and all", () => {
  const [pre] = markdown("```js\nconst a = **not bold**;\n<b>literal</b>\n```");
  assert.equal(pre.tagName, "pre");
  assert.equal(text(pre), "const a = **not bold**;\n<b>literal</b>");
  assert.equal(find(pre, "code")[0].getAttribute("data-lang"), "js");
  assert.equal(find(pre, "strong").length, 0);
});

check("an unclosed fence still renders to the end", () => {
  const [pre] = markdown("```\nnever closed");
  assert.equal(pre.tagName, "pre");
  assert.equal(text(pre), "never closed");
});

// ── inline ───────────────────────────────────────────────────────────────────────────────

check("bold, italic, strike and code", () => {
  assert.equal(shape(inline("**b**").at(0)), 'strong["b"]');
  assert.equal(shape(inline("*i*").at(0)), 'em["i"]');
  assert.equal(shape(inline("~~s~~").at(0)), 'del["s"]');
  assert.equal(shape(inline("`c`").at(0)), 'code.md-code["c"]');
});

check("code spans are not parsed further", () => {
  const [code] = inline("`**not bold** $x^2$`");
  assert.equal(text(code), "**not bold** $x^2$");
});

check("snake_case is not italicised", () => {
  const nodes = inline("a_variable_name here");
  assert.equal(nodes.filter((n) => n.tagName === "em").length, 0);
  assert.equal(nodes.map(text).join(""), "a_variable_name here");
});

check("a backslash escapes a marker", () => {
  assert.equal(inline("\\*not italic\\*").map(text).join(""), "*not italic*");
});

check("links keep their text and get a safe rel", () => {
  const [a] = inline("[docs](https://example.com/x)");
  assert.equal(a.tagName, "a");
  assert.equal(text(a), "docs");
  assert.equal(a.getAttribute("href"), "https://example.com/x");
  assert.equal(a.getAttribute("rel"), "noopener noreferrer");
});

check("a bare url becomes a link", () => {
  assert.equal(inline("see https://example.com now").filter((n) => n.tagName === "a").length, 1);
});

check("a javascript: url is refused, and shows what was there", () => {
  const nodes = inline("[click](javascript:alert(1))");
  assert.equal(nodes.filter((n) => n.tagName === "a").length, 0);
  // The source, not just the link text: a reader should be able to see that a link was
  // dropped and where it pointed.
  assert.ok(nodes.map(text).join("").startsWith("[click](javascript:alert(1)"));
});

// ── the security property ────────────────────────────────────────────────────────────────

check("html in the input is text in the output, everywhere", () => {
  const nasty = '<script>alert(1)</script><img src=x onerror=alert(1)>';
  for (const input of [nasty, `# ${nasty}`, `- ${nasty}`, `> ${nasty}`, `**${nasty}**`]) {
    const rendered = markdown(input);
    // No element of any kind was created from the input's markup: only text nodes carry it.
    assert.equal(findAll(rendered, "script").length, 0, `script from ${JSON.stringify(input)}`);
    assert.equal(findAll(rendered, "img").length, 0, `img from ${JSON.stringify(input)}`);
    assert.ok(text(rendered[0]).includes("alert(1)"), "the text itself survives");
  }
});

// ── maths ────────────────────────────────────────────────────────────────────────────────

const mathML = (src, display = false) => renderMath(src, display);

check("a superscript becomes msup", () => {
  assert.equal(shape(mathML("x^2")), 'math[mrow[msup[mi["x"],mn["2"]]]]');
});

check("sub and super on one base share a stack", () => {
  assert.equal(find(mathML("x_i^2"), "msubsup").length, 1);
});

check("a fraction becomes mfrac", () => {
  const frac = find(mathML("\\frac{a}{b}"), "mfrac");
  assert.equal(frac.length, 1);
  assert.equal(text(frac[0]), "ab");
});

check("greek letters resolve", () => {
  assert.equal(text(mathML("\\alpha + \\beta")), "α+β");
});

check("a sum carries its limits under and over", () => {
  const rendered = mathML("\\sum_{i=1}^{n} i");
  assert.equal(find(rendered, "munderover").length, 1);
  assert.ok(text(rendered).startsWith("∑"));
});

check("roots, with and without an index", () => {
  assert.equal(find(mathML("\\sqrt{2}"), "msqrt").length, 1);
  assert.equal(find(mathML("\\sqrt[3]{x}"), "mroot").length, 1);
});

check("text inside maths stays upright text", () => {
  assert.equal(text(find(mathML("\\text{if } x > 0"), "mtext")[0]), "if ");
});

check("conditioning, norms and fences that real write-ups use", () => {
  // \mid was missing the first time this met a real artifact, and a conditional probability
  // took the whole display equation down with it.
  assert.equal(text(mathML("p(y \\mid x)")), "p(y∣x)");
  assert.equal(text(mathML("\\|x\\|_2")), "‖x‖2");
  assert.equal(text(mathML("\\left[ a \\right]")), "[a]");
  assert.equal(text(mathML("\\langle a, b \\rangle")), "⟨a,b⟩");
});

check("a whole objective, of the kind a sweep write-up carries", () => {
  const src = "J(\\theta) = \\mathbb{E}_{x \\sim D}\\left[ \\sum_{t=1}^{T} " +
              "\\log \\pi_\\theta(a_t \\mid s_t) A_t \\right]";
  const rendered = mathML(src, true);
  assert.equal(rendered.tagName, "math", "should not fall back");
  assert.equal(find(rendered, "munderover").length, 1);
  assert.ok(text(rendered).includes("∣"));
});

check("manual bracket sizing keeps the bracket and drops the size", () => {
  // From a real write-up: MathML sizes fences itself, so \big is about emphasis rather than
  // meaning, and losing the emphasis beats falling back to raw source over it.
  const src = "h = h + \\alpha_1(c)\\,\\mathrm{MSA}\\big(\\mathrm{AdaLN}(h, c)\\big)";
  const rendered = mathML(src, true);
  assert.equal(rendered.tagName, "math", "should not fall back");
  assert.ok(text(rendered).includes("MSA"));
  assert.equal((text(rendered).match(/\(/g) || []).length, 3, "every bracket survives");
});

check("every sizing prefix is accepted", () => {
  for (const cmd of ["big", "Big", "bigg", "Bigg", "bigl", "Bigr", "biggl"]) {
    assert.equal(mathML(`\\${cmd}( x \\${cmd}[`).tagName, "math", cmd);
  }
});

check("environments become tables", () => {
  const cases = mathML("f(x) = \\begin{cases} 1 & x > 0 \\\\ 0 & x \\le 0 \\end{cases}", true);
  assert.equal(cases.tagName, "math", "should not fall back");
  assert.equal(find(cases, "mtr").length, 2, "two rows");
  assert.equal(find(cases, "mtd").length, 4, "two cells each");

  const aligned = mathML("\\begin{aligned} a &= b \\\\ c &= d \\end{aligned}", true);
  assert.equal(find(aligned, "mtr").length, 2);

  // A trailing row separator must not draw a blank line.
  assert.equal(find(mathML("\\begin{matrix} a \\\\ b \\\\ \\end{matrix}"), "mtr").length, 2);
});

check("a mismatched environment falls back rather than eating the document", () => {
  assert.equal(mathML("\\begin{aligned} a \\end{matrix}").getAttribute("class"), "math-raw");
  assert.equal(mathML("\\begin{nosuchenv} a \\end{nosuchenv}").getAttribute("class"), "math-raw");
});

check("the commands a real corpus actually uses", () => {
  // Each of these was found by rendering every expression in 2230 real .mdx files and
  // reading what came back as source. In frequency order.
  for (const src of [
    "\\operatorname{clip}(r_t, 1-\\epsilon, 1+\\epsilon)",
    "\\{o_1, \\dots, o_G\\}",
    "y_w \\succ y_l",
    "\\varnothing",
    "\\underbrace{a + b}_{c}",
    "\\arg\\min_\\theta L",
    "99\\%",
    "A \\Leftrightarrow B",
    "\\boxed{H(p, q)}",
    "\\texttt{blockIdx}",
    "\\boldsymbol{x}",
    "\\underset{x}{\\arg\\min}",
    "\\xrightarrow{f} y",
    "\\substack{i = 1 \\\\ j = 2}",
  ]) {
    assert.equal(mathML(src).tagName, "math", `should render: ${src}`);
  }
});

check("display maths is marked as display", () => {
  assert.equal(mathML("x", true).getAttribute("display"), "block");
  assert.equal(mathML("x", false).getAttribute("display"), "inline");
});

check("inline $…$ is picked up in prose", () => {
  const nodes = inline("the loss is $x^2$ here");
  assert.equal(nodes.filter((n) => n.tagName === "math").length, 1);
});

check("a lone dollar is not maths", () => {
  const nodes = inline("it cost $5 and $6");
  assert.equal(nodes.filter((n) => n.tagName === "math").length, 0);
  assert.equal(nodes.map(text).join(""), "it cost $5 and $6");
});

check("$$ on its own lines is a display block", () => {
  const [block] = markdown("$$\n\\frac{a}{b}\n$$");
  assert.equal(block.getAttribute("class"), "md-math");
  assert.equal(find(block, "mfrac").length, 1);
});

// The fallback is the point: a subset that fails loudly beats one that fails silently.
check("an unknown command falls back to its own source, visibly", () => {
  const raw = mathML("\\providecommand{x}");
  assert.equal(raw.tagName, "span");
  assert.equal(raw.getAttribute("class"), "math-raw");
  assert.equal(text(raw), "$\\providecommand{x}$");
  assert.ok(raw.getAttribute("title").includes("providecommand"));
});

check("unbalanced braces fall back rather than throwing", () => {
  assert.equal(mathML("\\frac{a}{b").getAttribute("class"), "math-raw");
  assert.equal(mathML("x}").getAttribute("class"), "math-raw");
});

check("the fallback keeps display fences for display maths", () => {
  assert.equal(text(mathML("\\nope", true)), "$$\\nope$$");
});

// ── frontmatter ──────────────────────────────────────────────────────────────────────────

check("frontmatter is read as metadata, not as a rule and some stray colons", () => {
  const [front, body] = markdown("---\ntitle: Sweep 3\ndraft: true\n---\n\nBody text");
  assert.equal(front.attrs.class, "md-front");
  assert.equal(find(front, "div").filter((d) => d.attrs.class === "md-front-row").length, 2);
  assert.ok(text(front).includes("title"));
  assert.equal(text(body), "Body text");
});

check("a rule that is not frontmatter is still a rule", () => {
  assert.equal(markdown("above\n\n---\n\nbelow")[1].tagName, "hr");
});

check("an unterminated frontmatter fence is left alone", () => {
  assert.equal(markdown("---\ntitle: x\n\nbody").at(0).tagName, "hr");
});

// ── mdx ──────────────────────────────────────────────────────────────────────────────────

check("mdx prose renders as markdown", () => {
  const blocks = mdx("# Title\n\nSome **bold** prose.");
  assert.deepEqual(blocks.map((b) => b.tagName), ["h3", "p"]);
  assert.equal(findAll(blocks, "strong").length, 1);
});

check("imports are collected and folded away rather than read as prose", () => {
  const [module, body] = mdx('import Chart from "./chart"\nexport const meta = {a: 1}\n\nProse.');
  assert.equal(module.tagName, "details");
  assert.equal(module.attrs.class, "md-module");
  assert.ok(text(module).includes("2 import/export lines"));
  assert.equal(text(body), "Prose.");
});

check("a component wrapping prose keeps the prose", () => {
  const [node] = mdx("<Callout type=\"warn\">\n\nThe split is **stale**.\n\n</Callout>");
  assert.equal(node.attrs.class, "mdx-node");
  assert.ok(text(node).includes("<Callout>"));
  assert.ok(text(node).includes('type="warn"'));
  // The content survives, rendered as what it is.
  assert.equal(findAll([node], "strong").length, 1);
  assert.ok(text(node).includes("The split is"));
});

check("a self-closing component is named with its props", () => {
  const [node] = mdx('<Chart data={rows} height={200} />');
  assert.equal(node.attrs.class, "mdx-node");
  assert.ok(text(node).includes("<Chart>"));
  assert.ok(text(node).includes("data={rows}"));
});

check("nesting closes the right box", () => {
  const [outer] = mdx("<Box>\n<Box>\ninner\n</Box>\nafter\n</Box>");
  // `find` includes the root, so this is the outer plus exactly one nested box — the
  // closing tag matched the inner one rather than ending the outer early.
  assert.equal(find(outer, "div").filter((d) => d.attrs.class === "mdx-node").length, 2);
  const body = find(outer, "div").find((d) => d.attrs.class === "mdx-body");
  assert.deepEqual(
    body.childNodes.map((c) => c.attrs.class || c.tagName),
    ["mdx-node", "md-p"],
    "the inner box, then the text that followed it — both inside the outer",
  );
  assert.equal(text(body.childNodes[1]), "after");
});

check("a jsx expression is shown, never evaluated", () => {
  const nodes = inline("the value is {props.count} today", true);
  const expr = nodes.find((n) => n.attrs && n.attrs.class === "mdx-expr");
  assert.ok(expr, "expression marked");
  assert.equal(text(expr), "{props.count}");
  assert.ok(expr.getAttribute("title").includes("Not evaluated"));
});

check("braces in ordinary markdown are left alone", () => {
  assert.equal(inline("a {literal} brace").map(text).join(""), "a {literal} brace");
  assert.equal(inline("a {literal} brace").filter((n) => n.tagName === "code").length, 0);
});

check("a lowercase tag is not treated as a component, and never becomes html", () => {
  const blocks = mdx("<script>alert(1)</script>");
  assert.equal(findAll(blocks, "script").length, 0);
  assert.ok(text(blocks[0]).includes("alert(1)"));
});

check("mdx markup in a plain markdown file stays literal", () => {
  const blocks = markdown("<Callout>\nhi\n</Callout>");
  assert.equal(findAll(blocks, "div").filter((d) => d.attrs.class === "mdx-node").length, 0);
  assert.ok(text(blocks[0]).includes("<Callout>"));
});

// ── report ───────────────────────────────────────────────────────────────────────────────

if (failures.length) {
  console.log(`${passed} passed, ${failures.length} FAILED\n`);
  for (const f of failures) console.log(`  ✗ ${f}\n`);
  process.exit(1);
}
console.log(`markdown: ${passed} passed`);
