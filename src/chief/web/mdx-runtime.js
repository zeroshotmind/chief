/* Rendering MDX with its components — a JSX transform and a small component runtime.

   This file runs *inside the sandboxed frame*, never in the page that reads the run. It is
   deliberately a classic script rather than a module: the frame is at an opaque origin, and
   a module fetch from there would need CORS that Chief does not serve. It defines one
   global, `ChiefMDX`.

   Why hand-written rather than React plus a compiler: React 19 ships no UMD build and
   Sucrase no browser bundle, so vendoring either means adding a build step to a project
   whose UI is deliberately five static files with none. And it turns out neither is needed
   at this size. What agent-written components actually use is function components and five
   hooks, and what JSX needs is not a JavaScript parser but a brace-balancer — the browser
   evaluates the expressions, this only has to find where they end.

   The rule the LaTeX translator set holds here too: **fail loudly**. A parse this does not
   understand throws, the caller shows the source, and nothing renders a plausible
   approximation of a component that never ran. */

(function (global) {
  "use strict";

  // ── the element tree ───────────────────────────────────────────────────────────────────

  const FRAGMENT = { fragment: true };

  function h(type, props, ...children) {
    return { type, props: props || {}, children: children.flat(Infinity) };
  }

  // ── hooks ──────────────────────────────────────────────────────────────────────────────
  //
  // One cell list per component instance, indexed by call order — the same bargain React
  // makes, and the reason hooks cannot sit inside a condition. Instances are keyed by their
  // position in the tree, so a re-render finds its own state again.

  let current = null;

  function hookCell(initial) {
    const cells = current.cells;
    if (cells.length <= current.index) cells.push({ value: initial });
    return cells[current.index++];
  }

  function useState(initial) {
    const cell = hookCell(typeof initial === "function" ? initial() : initial);
    const owner = current.instance;
    const set = (next) => {
      const value = typeof next === "function" ? next(cell.value) : next;
      if (Object.is(value, cell.value)) return;
      cell.value = value;
      owner.schedule();
    };
    return [cell.value, set];
  }

  const same = (a, b) => a && b && a.length === b.length && a.every((v, i) => Object.is(v, b[i]));

  function useMemo(fn, deps) {
    const cell = hookCell(undefined);
    if (!cell.deps || !same(cell.deps, deps)) {
      cell.deps = deps;
      cell.value = fn();
    }
    return cell.value;
  }

  const useCallback = (fn, deps) => useMemo(() => fn, deps);
  const useRef = (initial) => hookCell({ current: initial }).value;

  function useEffect(fn, deps) {
    const cell = hookCell(undefined);
    if (!cell.deps || !same(cell.deps, deps)) {
      cell.deps = deps;
      // After the tree is in the document, so an effect measuring a node finds one.
      current.instance.effects.push(() => {
        if (typeof cell.cleanup === "function") cell.cleanup();
        cell.cleanup = fn();
      });
    }
  }

  const useReducer = (reduce, initial) => {
    const [state, set] = useState(initial);
    return [state, (action) => set((s) => reduce(s, action))];
  };

  // ── rendering ──────────────────────────────────────────────────────────────────────────

  // Attributes that are not attributes. `className` and `htmlFor` are JSX's names for two
  // reserved words; the rest are properties that must be assigned rather than set.
  const PROPERTY = { value: 1, checked: 1, selected: 1, muted: 1 };

  /** Anything that could execute is refused. The frame is sandboxed and cannot reach Chief,
      but a component that quietly accepts `dangerouslySetInnerHTML` is a component whose
      output nobody can reason about. */
  function setProp(node, name, value) {
    if (name === "children" || name === "key" || name === "ref") return;
    if (name === "dangerouslySetInnerHTML") {
      throw new Error("dangerouslySetInnerHTML is not supported");
    }
    if (name.startsWith("on") && typeof value === "function") {
      node.addEventListener(name.slice(2).toLowerCase(), value);
      return;
    }
    if (name === "style" && value && typeof value === "object") {
      Object.assign(node.style, value);
      return;
    }
    const attr = name === "className" ? "class" : name === "htmlFor" ? "for" : name;
    if (value == null || value === false) return;
    if (PROPERTY[attr]) {
      node[attr] = value;
      return;
    }
    node.setAttribute(attr, value === true ? "" : String(value));
  }

  /** One tree into real nodes. `instance` carries the state cells for the subtree. */
  function build(vnode, instance, path) {
    if (vnode == null || vnode === false || vnode === true) return [];
    if (Array.isArray(vnode)) return vnode.flatMap((v, i) => build(v, instance, `${path}.${i}`));
    if (typeof vnode !== "object") return [document.createTextNode(String(vnode))];
    if (vnode.dom) return [vnode.dom]; // a node the markdown renderer already built

    const { type, props, children } = vnode;

    if (type === FRAGMENT) return build(children, instance, path);

    if (typeof type === "function") {
      // A component: its own hook cells, found again on re-render by its path in the tree.
      const key = `${path}/${type.name || "anon"}`;
      const cells = instance.state.get(key) || [];
      instance.state.set(key, cells);
      const previous = current;
      current = { cells, index: 0, instance };
      let out;
      try {
        out = type({ ...props, children });
      } finally {
        current = previous;
      }
      return build(out, instance, key);
    }

    const node = document.createElement(String(type));
    for (const [name, value] of Object.entries(props)) setProp(node, name, value);
    for (const child of build(children, instance, path)) node.appendChild(child);
    return [node];
  }

  /** Mount a tree into `host`, re-rendering it whenever a hook says the state moved. */
  function render(vnode, host) {
    const instance = {
      state: new Map(),
      effects: [],
      schedule() {
        // Coalesced: several setState calls in one handler are one re-render, which is both
        // faster and the only way a component that sets two pieces of state stays coherent.
        if (instance.queued) return;
        instance.queued = true;
        Promise.resolve().then(() => {
          instance.queued = false;
          draw();
        });
      },
    };
    const draw = () => {
      instance.effects = [];
      const nodes = build(vnode, instance, "");
      host.replaceChildren(...nodes);
      for (const effect of instance.effects) effect();
    };
    draw();
    return instance;
  }

  global.ChiefMDX = {
    h, FRAGMENT, render,
    hooks: { useState, useMemo, useCallback, useRef, useEffect, useReducer },
  };
})(typeof globalThis === "undefined" ? this : globalThis);

/* ── MDX into a component ──────────────────────────────────────────────────────────────────
 *
 * The document is split into markdown runs and JSX blocks, the markdown rendered by the same
 * renderer the rest of Chief uses, and the JSX compiled and evaluated with the co-located
 * modules in scope. What comes out is one tree, so a component's own state works exactly as
 * it would anywhere else.
 */

(function (global) {
  "use strict";

  const { h, FRAGMENT, hooks } = global.ChiefMDX;

  /** Evaluate one module's source, resolving its co-located imports from `loaded`. */
  function evaluateModule(source, resolve) {
    const imports = [];
    // Rewritten rather than parsed: `import { A as B } from "./x"` becomes a destructure of
    // whatever `resolve` returns. Only the forms an agent-written component actually uses.
    const body = source.replace(
      /^\s*import\s+(?:([\w$]+)\s*,\s*)?(?:\{([^}]*)\}|\*\s*as\s+([\w$]+)|([\w$]+))?\s*from\s*['"]([^'"]+)['"];?/gm,
      (_all, both, named, star, plain, spec) => {
        const target = `__m${imports.length}`;
        imports.push({ spec, target });
        const lines = [];
        const dflt = both || plain;
        if (dflt) lines.push(`const ${dflt} = ${target}.default ?? ${target};`);
        if (star) lines.push(`const ${star} = ${target};`);
        if (named) {
          const fields = named.split(",").map((piece) => {
            const [from, to] = piece.split(/\s+as\s+/).map((x) => x.trim());
            return to ? `${from}: ${to}` : from;
          }).filter(Boolean).join(", ");
          if (fields) lines.push(`const { ${fields} } = ${target};`);
        }
        return lines.join(" ");
      },
    );

    // `export` is stripped rather than honoured: what a module exports is collected from the
    // names it declared, which is enough for components and avoids a second parser.
    const names = [];
    const stripped = body.replace(/^\s*export\s+(default\s+)?/gm, (_a, isDefault) => {
      if (isDefault) names.push("default");
      return "";
    }).replace(/^\s*(?:const|let|var|function|class)\s+([\w$]+)/gm, (all, name) => {
      names.push(name);
      return all;
    });

    const scope = imports.map((i) => i.target);
    const values = imports.map((i) => resolve(i.spec));
    const collect = `return {${names.filter((n) => n !== "default").map((n) => `${n}`).join(",")}};`;
    const factory = new Function(
      "H", "F", ...Object.keys(hooks), ...scope,
      `"use strict";${global.ChiefJSX.transform(stripped)}\n${collect}`,
    );
    return factory(h, FRAGMENT, ...Object.values(hooks), ...values);
  }

  /** The whole graph, entry last, each module evaluated once. */
  function loadModules(modules, entry) {
    const done = new Map();
    const resolve = (spec) => {
      if (done.has(spec)) return done.get(spec);
      const source = modules[spec] ?? modules[`${spec}.jsx`] ?? modules[`${spec}.js`];
      if (source === undefined) throw new Error(`no module ${spec} beside this document`);
      const value = evaluateModule(source, resolve);
      done.set(spec, value);
      return value;
    };
    for (const key of Object.keys(modules)) if (key !== entry) resolve(key);
    return { resolve, done };
  }

  /** Compile and render an MDX document into `host`.

      `markdown` is Chief's own renderer, passed in rather than imported — this file is a
      classic script and cannot import one. */
  function renderMdx({ entry, modules, host, markdown }) {
    const source = modules[entry];
    if (source === undefined) throw new Error(`no entry module ${entry}`);
    const { resolve } = loadModules(modules, entry);

    // Everything the document imported, by the name it bound. The same rewriting as a
    // module, but the result is a scope for the JSX blocks rather than a module's exports.
    const scope = {};
    const withoutImports = source.replace(
      /^\s*import\s+(?:([\w$]+)\s*,\s*)?(?:\{([^}]*)\}|\*\s*as\s+([\w$]+)|([\w$]+))?\s*from\s*['"]([^'"]+)['"];?/gm,
      (_all, both, named, star, plain, spec) => {
        let module;
        try {
          module = resolve(spec);
        } catch {
          return ""; // a bare specifier like `react`: the runtime is already in scope
        }
        const dflt = both || plain;
        if (dflt) scope[dflt] = module.default ?? module;
        if (star) scope[star] = module;
        if (named) {
          for (const piece of named.split(",")) {
            const [from, to] = piece.split(/\s+as\s+/).map((x) => x.trim());
            if (from) scope[to || from] = module[from];
          }
        }
        return "";
      },
    ).replace(/^\s*export\s+/gm, "");

    global.ChiefMDX.render(h(FRAGMENT, null, blocksOf(withoutImports, scope, markdown)), host);
  }

  /** Markdown runs as ready-made nodes, JSX blocks compiled — in document order. */
  function blocksOf(source, scope, markdown) {
    return markdown(source, { mdx: true, nodes: true }).map((block) =>
      block.jsx === undefined ? { dom: block } : compileBlock(block.jsx, scope, markdown),
    );
  }

  /** One component invocation.

      The opening tag is compiled on its own to get the component and its props; the children
      go back through the markdown renderer, because in MDX they are markdown and not text.
      That recursion is what makes `<Callout>` around three paragraphs read as three
      paragraphs rather than one long string. */
  function compileBlock(spec, scope, markdown) {
    const { tag, props, body } = spec;
    const keys = Object.keys(scope);
    const factory = new Function(
      "H", "F", ...Object.keys(hooks), ...keys,
      `"use strict";return (${global.ChiefJSX.transform(`<${tag}${props || ""} />`)});`,
    );
    const element = factory(h, FRAGMENT, ...Object.values(hooks), ...keys.map((k) => scope[k]));
    if (body == null || !body.trim()) return element;
    return { ...element, children: blocksOf(body, scope, markdown) };
  }

  global.ChiefMDX.renderMdx = renderMdx;
  global.ChiefMDX.evaluateModule = evaluateModule;
})(typeof globalThis === "undefined" ? this : globalThis);
