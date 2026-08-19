/* Unit tests for the JSX transform and the component runtime.

       node scripts/test_jsx.mjs

   Two halves. The transform is checked by what it emits *and* by evaluating the result —
   a transform that produces plausible-looking JavaScript which then throws is worse than
   one that fails at transform time, so both are asserted.

   The runtime is checked against a stub DOM, the same way the markdown renderer is. What
   matters most is the hook cases: state surviving a re-render, and finding its way back to
   the right component when the tree has several.
*/

import assert from "node:assert";
import { readFileSync } from "node:fs";
import vm from "node:vm";

// ── a stub DOM, shared by both halves ─────────────────────────────────────────────────────

function makeDom() {
  const listeners = [];
  function node(tag) {
    const self = {
      tag, attrs: {}, style: {}, children: [], listeners: [],
      setAttribute(k, v) { self.attrs[k] = String(v); },
      getAttribute(k) { return self.attrs[k] ?? null; },
      appendChild(c) { self.children.push(c); return c; },
      replaceChildren(...c) { self.children = c; },
      addEventListener(t, fn) { self.listeners.push({ type: t, fn }); listeners.push({ node: self, type: t, fn }); },
    };
    return self;
  }
  return {
    listeners,
    document: { createElement: node, createTextNode: (t) => ({ text: String(t) }) },
  };
}

const load = (file, sandbox) => {
  const context = vm.createContext(sandbox);
  vm.runInContext(readFileSync(new URL(file, import.meta.url), "utf8"), context);
  return context;
};

const dom = makeDom();
const sandbox = { document: dom.document, Promise, Object, Array, String, Number, Math, JSON, console };
sandbox.globalThis = sandbox;
load("../src/chief/web/jsx.js", sandbox);
load("../src/chief/web/mdx-runtime.js", sandbox);
const { transform, JsxError } = sandbox.ChiefJSX;
const { h: H, FRAGMENT: F, render, hooks } = sandbox.ChiefMDX;

let passed = 0;
const failures = [];
function check(name, fn) {
  try { fn(); passed += 1; } catch (err) {
    failures.push(`${name}\n    ${err.message.split("\n").join("\n    ")}`);
  }
}

/** Transform a source and evaluate it, with the runtime in scope. */
function run(source, scope = {}) {
  const js = transform(source);
  const keys = ["H", "F", ...Object.keys(hooks), ...Object.keys(scope)];
  const values = [H, F, ...Object.values(hooks), ...Object.values(scope)];
  return new Function(...keys, `"use strict";return (${js});`)(...values);
}

const text = (n) => (n.text != null ? n.text : (n.children || []).map(text).join(""));
const find = (n, tag, out = []) => {
  if (n.tag === tag) out.push(n);
  for (const c of n.children || []) find(c, tag, out);
  return out;
};

// ── the transform ─────────────────────────────────────────────────────────────────────────

check("an element becomes a call", () => {
  assert.equal(transform("<div />"), 'H("div",null)');
  assert.equal(transform("<div>hi</div>"), 'H("div",null,"hi")');
});

check("a capitalised tag is a reference, a lowercase one a string", () => {
  assert.ok(transform("<Chart />").startsWith("H(Chart,"));
  assert.ok(transform("<div />").startsWith('H("div",'));
  assert.ok(transform("<Ns.Thing />").startsWith("H(Ns.Thing,"));
});

check("attributes: strings, expressions, bare, and spread", () => {
  assert.equal(transform('<a b="c" />'), 'H("a",{"b":("c")})');
  assert.equal(transform("<a b={1 + 2} />"), 'H("a",{"b":(1 + 2)})');
  assert.equal(transform("<a disabled />"), 'H("a",{"disabled":(true)})');
  assert.ok(transform("<a {...rest} b={1} />").includes("...(rest)"));
});

check("an expression containing braces or a string is not cut short", () => {
  const js = transform('<a b={ {x: "}"} } c={`a${"}"}b`} />');
  assert.ok(js.includes('{x: "}"}'), js);
  assert.ok(js.includes("`a${\"}\"}b`"), js);
});

check("nesting and fragments", () => {
  assert.equal(transform("<a><b /></a>"), 'H("a",null,H("b",null))');
  assert.equal(transform("<><a /></>"), 'H(F,null,H("a",null))');
});

check("whitespace-only lines between elements are dropped", () => {
  assert.equal(transform("<a>\n  <b />\n</a>"), 'H("a",null,H("b",null))');
});

check("a jsx comment child is not a child", () => {
  assert.equal(transform("<a>{/* nothing */}</a>"), 'H("a",null)');
});

check("less-than is still less-than", () => {
  assert.equal(transform("a < b"), "a < b");
  assert.equal(transform("x = i<j ? 1 : 2"), "x = i<j ? 1 : 2");
  assert.ok(transform("return <div />").includes('H("div"'));
});

check("jsx inside strings and comments is left alone", () => {
  assert.equal(transform('const s = "<div>"'), 'const s = "<div>"');
  assert.equal(transform("// <div>\nx"), "// <div>\nx");
  assert.equal(transform("/* <div> */ x"), "/* <div> */ x");
});

check("a mismatched closing tag throws rather than guessing", () => {
  assert.throws(() => transform("<a></b>"), JsxError);
  assert.throws(() => transform("<a>"), JsxError);
  assert.throws(() => transform("<a b={ />"), JsxError);
});

// ── the transform's output actually runs ──────────────────────────────────────────────────

check("what it emits evaluates to a tree", () => {
  const tree = run('<div className="x"><span>hi</span></div>');
  assert.equal(tree.type, "div");
  assert.equal(tree.props.className, "x");
  assert.equal(tree.children[0].type, "span");
});

check("expressions are evaluated by the browser, not by us", () => {
  // And JSX *inside* an expression is transformed too — `items.map(i => <Row/>)` is the
  // ordinary way a list is written, and leaving it alone emits source that will not parse.
  const tree = run("<b>{1 + 2}{items.map((i) => <i key={i}>{i}</i>)}</b>", { items: [1, 2] });
  // `h` flattens, so the mapped array arrives spread rather than nested.
  assert.equal(tree.children.length, 3);
  assert.equal(tree.children[0], 3);
  assert.deepEqual(tree.children.slice(1).map((c) => c.type), ["i", "i"]);
});

check("jsx nested in an attribute value is transformed", () => {
  const tree = run("<Table render={() => <Row />} />", { Table: (p) => p, Row: () => null });
  assert.equal(typeof tree.props.render, "function");
  assert.equal(tree.props.render().type.name, "Row");
});

// ── the runtime ───────────────────────────────────────────────────────────────────────────

check("elements, attributes and text reach the dom", () => {
  const host = dom.document.createElement("div");
  render(run('<section className="a" id="b">text</section>'), host);
  const [node] = host.children;
  assert.equal(node.tag, "section");
  assert.equal(node.attrs.class, "a", "className becomes class");
  assert.equal(node.attrs.id, "b");
  assert.equal(text(node), "text");
});

check("a function component renders, and receives props and children", () => {
  const Box = ({ title, children }) => run("<div><h2>{title}</h2>{children}</div>", { title, children });
  const host = dom.document.createElement("div");
  render(run("<Box title={'T'}><p>inner</p></Box>", { Box }), host);
  assert.equal(find(host, "h2").length, 1);
  assert.equal(text(find(host, "h2")[0]), "T");
  assert.equal(text(find(host, "p")[0]), "inner");
});

check("false and null children render nothing", () => {
  const host = dom.document.createElement("div");
  render(run("<div>{false}{null}{0}</div>"), host);
  assert.equal(text(host.children[0]), "0", "0 is a value, false and null are not");
});

check("useState survives a re-render, per component", async () => {
  const Counter = ({ start }) => {
    const [n, setN] = hooks.useState(start);
    return run("<button onClick={() => setN(n + 1)}>{n}</button>", { n, setN });
  };
  const host = dom.document.createElement("div");
  render(run("<div><Counter start={0} /><Counter start={10} /></div>", { Counter }), host);

  const buttons = find(host, "button");
  assert.deepEqual(buttons.map(text), ["0", "10"]);

  buttons[0].listeners[0].fn();
  await Promise.resolve();
  await Promise.resolve();
  // The one that was clicked moved; its neighbour kept its own state.
  assert.deepEqual(find(host, "button").map(text), ["1", "10"]);
});

check("useMemo recomputes only when its dependencies change", () => {
  let calls = 0;
  const Memo = ({ a }) => {
    const value = hooks.useMemo(() => { calls += 1; return a * 2; }, [a]);
    return run("<i>{value}</i>", { value });
  };
  const host = dom.document.createElement("div");
  const tree = run("<Memo a={2} />", { Memo });
  render(tree, host);
  render(tree, host);
  assert.equal(calls, 2, "a fresh mount recomputes");
  assert.equal(text(host.children[0]), "4");
});

check("useEffect runs after the tree is in place, and cleans up", async () => {
  const seen = [];
  const Eff = () => {
    const [n, setN] = hooks.useState(0);
    hooks.useEffect(() => {
      seen.push(`run ${n}`);
      return () => seen.push(`clean ${n}`);
    }, [n]);
    return run("<b onClick={() => setN(1)}>{n}</b>", { n, setN });
  };
  const host = dom.document.createElement("div");
  render(run("<Eff />", { Eff }), host);
  assert.deepEqual(seen, ["run 0"]);
  find(host, "b")[0].listeners[0].fn();
  await Promise.resolve();
  await Promise.resolve();
  assert.deepEqual(seen, ["run 0", "clean 0", "run 1"]);
});

check("useRef keeps the same object across renders", () => {
  const boxes = [];
  const R = () => {
    const [n, setN] = hooks.useState(0);
    boxes.push(hooks.useRef({ id: 1 }));
    return run("<b onClick={() => setN(n + 1)}>{n}</b>", { n, setN });
  };
  const host = dom.document.createElement("div");
  render(run("<R />", { R }), host);
  find(host, "b")[0].listeners[0].fn();
  return Promise.resolve().then(() => Promise.resolve()).then(() => {
    assert.ok(boxes.length >= 2);
    assert.strictEqual(boxes[0], boxes[1], "the same ref object");
  });
});

check("dangerouslySetInnerHTML is refused", () => {
  const host = dom.document.createElement("div");
  assert.throws(() => render(H("div", { dangerouslySetInnerHTML: { __html: "<b>x</b>" } }), host),
    /dangerouslySetInnerHTML/);
});

// ── mdx end to end ────────────────────────────────────────────────────────────────────────
//
// The whole path: a document, a component beside it, compiled and rendered together, with
// the component's own state working.

// The markdown renderer builds nodes in *this* realm while the runtime builds them inside
// the vm sandbox. They share one stub document, so the two sets of nodes are the same kind
// of thing — which is exactly the arrangement in the browser, where the frame has one DOM.
globalThis.document = dom.document;
globalThis.document.createElementNS = (_ns, tag) => dom.document.createElement(tag);
const { markdown } = await import("../src/chief/web/markdown.js");

const COUNTER = `
export function Counter({ start, label }) {
  const [n, setN] = useState(start)
  return (
    <div className="counter">
      <span>{label}: {n}</span>
      <button onClick={() => setN(n + 1)}>+</button>
    </div>
  )
}
`;

const DOC = `## Findings

Prose with **bold** in it.

<Callout kind="warn">

Inside the component, *markdown still works*.

</Callout>

<Counter start={2} label="clicks" />

After.
`;

const CALLOUT = `
export const Callout = ({ kind, children }) => (
  <aside className={"callout " + kind}>{children}</aside>
)
`;

check("an mdx document renders its markdown and runs its components", () => {
  const host = dom.document.createElement("div");
  sandbox.ChiefMDX.renderMdx({
    entry: "doc.mdx",
    modules: {
      "doc.mdx": `import { Callout } from "./Callout"
import { Counter } from "./Counter"

${DOC}`,
      "./Callout": CALLOUT,
      "./Counter": COUNTER,
    },
    host,
    markdown,
  });

  // Markdown around the components.
  assert.equal(find(host, "h4").length, 1, "the heading rendered (## is h4 here)");
  assert.ok(text(host).includes("Prose with"));
  assert.ok(text(host).includes("After."));

  // The component ran, with its props.
  const aside = find(host, "aside")[0];
  assert.ok(aside, "Callout rendered as an <aside>");
  assert.equal(aside.attrs.class, "callout warn");
  // And its children were markdown, not text.
  assert.equal(find(aside, "em").length, 1, "*markdown still works* is emphasised");

  const counter = find(host, "div").find((d) => d.attrs.class === "counter");
  assert.ok(counter, "Counter rendered");
  assert.ok(text(counter).includes("clicks: 2"), text(counter));
});

check("a component in an mdx document keeps its own state", async () => {
  const host = dom.document.createElement("div");
  sandbox.ChiefMDX.renderMdx({
    entry: "doc.mdx",
    modules: {
      "doc.mdx": `import { Counter } from "./Counter"

<Counter start={0} label="n" />
`,
      "./Counter": COUNTER,
    },
    host,
    markdown,
  });
  find(host, "button")[0].listeners[0].fn();
  await Promise.resolve();
  await Promise.resolve();
  assert.ok(text(host).includes("n: 1"), text(host));
});

check("a missing sibling module is reported, not silently skipped", () => {
  const host = dom.document.createElement("div");
  assert.throws(
    () => sandbox.ChiefMDX.renderMdx({
      entry: "doc.mdx",
      modules: { "doc.mdx": 'import { Gone } from "./Gone"\n\n<Gone />\n' },
      host, markdown,
    }),
    /Gone/,
  );
});

// ── report ────────────────────────────────────────────────────────────────────────────────

await Promise.resolve();
if (failures.length) {
  console.log(`${passed} passed, ${failures.length} FAILED\n`);
  for (const f of failures) console.log(`  ✗ ${f}\n`);
  process.exit(1);
}
console.log(`jsx + runtime: ${passed} passed`);
