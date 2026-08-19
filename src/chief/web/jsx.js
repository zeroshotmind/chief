/* JSX into plain JavaScript, without a JavaScript parser.

   Also a classic script, for the reason `mdx-runtime.js` is: it runs in a frame at an opaque
   origin where a module fetch would need CORS. It defines `ChiefJSX`.

   The trick that makes this small enough to hand-write: **JSX does not need its expressions
   parsed.** Everything inside `{…}` is JavaScript that the browser will evaluate; this only
   has to find where each one ends, which is brace-balancing with an awareness of strings,
   template literals, comments and regex-vs-divide. So what follows is a scanner, not a
   parser, and it is strict — anything it cannot account for throws, and the caller shows
   the source rather than rendering a guess.

   Deliberately not handled: TypeScript. Stripping types correctly needs the parser this
   avoids being, and a component written beside its own document has no need of them. */

(function (global) {
  "use strict";

  const IDENT = /[A-Za-z_$][\w$]*/y;
  const TAG_NAME = /[A-Za-z_$][\w$]*(?:[.:-][A-Za-z_$][\w$-]*)*/y;

  class JsxError extends Error {}

  /** Scan forward from `i` over one balanced `{…}`, returning the index just past it. */
  function balanced(src, i, open = "{", close = "}") {
    let depth = 0;
    while (i < src.length) {
      const c = src[i];
      if (c === "/" && src[i + 1] === "/") {
        i = src.indexOf("\n", i);
        if (i === -1) throw new JsxError("unterminated comment");
        continue;
      }
      if (c === "/" && src[i + 1] === "*") {
        const end = src.indexOf("*/", i + 2);
        if (end === -1) throw new JsxError("unterminated comment");
        i = end + 2;
        continue;
      }
      if (c === '"' || c === "'" || c === "`") {
        i = skipString(src, i);
        continue;
      }
      if (c === open) depth += 1;
      if (c === close) {
        depth -= 1;
        if (depth === 0) return i + 1;
      }
      i += 1;
    }
    throw new JsxError(`unbalanced ${open}`);
  }

  /** Past a string or template literal, including any `${…}` inside one. */
  function skipString(src, i) {
    const quote = src[i];
    i += 1;
    while (i < src.length) {
      const c = src[i];
      if (c === "\\") {
        i += 2;
        continue;
      }
      if (quote === "`" && c === "$" && src[i + 1] === "{") {
        i = balanced(src, i + 1);
        continue;
      }
      if (c === quote) return i + 1;
      i += 1;
    }
    throw new JsxError("unterminated string");
  }

  const match = (src, i, re) => {
    re.lastIndex = i;
    return re.exec(src);
  };

  /** Is the `<` at `i` the start of a tag, or a comparison?

      Decided by what came before it, which is how every JSX transform decides: an operator,
      an opening bracket, a comma, a keyword like `return` — then a tag. An identifier, a
      number or a closing bracket — then it is less-than. */
  function startsTag(src, i) {
    let j = i - 1;
    while (j >= 0 && /\s/.test(src[j])) j -= 1;
    if (j < 0) return true;
    const c = src[j];
    if ("([{,;=:?&|!+-*/%<>~^".includes(c)) return true;
    if (/[\w$)\]]/.test(c)) {
      // `return <div/>` and `=> <div/>` are tags; `a <b` is not.
      const word = /[\w$]+$/.exec(src.slice(0, j + 1));
      return !!word && ["return", "typeof", "in", "of", "await", "yield", "default"].includes(word[0]);
    }
    return true;
  }

  /** One element, from its `<`. Returns `{ js, end }`. */
  function element(src, i) {
    if (src[i] !== "<") throw new JsxError("expected <");
    // A fragment: <>…</>
    if (src[i + 1] === ">") {
      const inner = children(src, i + 2, "");
      return { js: `H(F,null${inner.js})`, end: inner.end };
    }
    const name = match(src, i + 1, TAG_NAME);
    if (!name) throw new JsxError("expected a tag name");
    let j = name.index + name[0].length;
    const parts = [];
    let spread = false;

    for (;;) {
      while (j < src.length && /\s/.test(src[j])) j += 1;
      if (src[j] === "/" && src[j + 1] === ">") {
        return { js: `H(${tagRef(name[0])},${propsOf(parts, spread)})`, end: j + 2 };
      }
      if (src[j] === ">") {
        const inner = children(src, j + 1, name[0]);
        return { js: `H(${tagRef(name[0])},${propsOf(parts, spread)}${inner.js})`, end: inner.end };
      }
      if (src[j] === "{") {
        // {...rest}
        const end = balanced(src, j);
        const body = src.slice(j + 1, end - 1).trim();
        if (!body.startsWith("...")) throw new JsxError("only {...spread} is allowed as an attribute");
        parts.push({ spread: transform(body.slice(3)) });
        spread = true;
        j = end;
        continue;
      }
      const attr = match(src, j, TAG_NAME);
      if (!attr) throw new JsxError(`unexpected ${JSON.stringify(src[j] ?? "end")} in a tag`);
      j = attr.index + attr[0].length;
      while (j < src.length && /\s/.test(src[j])) j += 1;
      if (src[j] !== "=") {
        parts.push({ key: attr[0], value: "true" }); // bare attribute
        continue;
      }
      j += 1;
      while (j < src.length && /\s/.test(src[j])) j += 1;
      if (src[j] === "{") {
        const end = balanced(src, j);
        // Recursively, because an attribute value is arbitrary JavaScript and may itself
        // hold JSX — `render={() => <Row />}` is ordinary.
        parts.push({ key: attr[0], value: transform(src.slice(j + 1, end - 1)) });
        j = end;
      } else if (src[j] === '"' || src[j] === "'") {
        const end = skipString(src, j);
        parts.push({ key: attr[0], value: JSON.stringify(src.slice(j + 1, end - 1)) });
        j = end;
      } else if (src[j] === "<") {
        const nested = element(src, j);
        parts.push({ key: attr[0], value: nested.js });
        j = nested.end;
      } else {
        throw new JsxError(`attribute ${attr[0]} has no value`);
      }
    }
  }

  // A capitalised or dotted name is a component in scope; anything else is a DOM tag name.
  const tagRef = (name) => (/^[a-z][\w-]*$/.test(name) ? JSON.stringify(name) : name);

  function propsOf(parts, spread) {
    if (!parts.length) return "null";
    if (!spread) {
      return `{${parts.map((p) => `${JSON.stringify(p.key)}:(${p.value})`).join(",")}}`;
    }
    const chunks = parts.map((p) =>
      p.spread ? `...(${p.spread})` : `${JSON.stringify(p.key)}:(${p.value})`,
    );
    return `{${chunks.join(",")}}`;
  }

  /** Children up to the closing tag, returned as `,child,child` for splicing into a call. */
  function children(src, i, name) {
    const out = [];
    let text = "";
    const flush = () => {
      // JSX drops whitespace-only runs that contain a newline, and trims the ends of the
      // rest. Without this every indented line becomes a stray text node.
      const trimmed = text.replace(/^[ \t]*\n\s*/, "").replace(/\s*\n[ \t]*$/, "");
      if (trimmed.trim()) out.push(JSON.stringify(trimmed));
      text = "";
    };
    while (i < src.length) {
      if (src[i] === "<" && src[i + 1] === "/") {
        flush();
        const close = match(src, i + 2, TAG_NAME);
        const closing = close ? close[0] : "";
        if (closing !== name) throw new JsxError(`</${closing}> closes <${name}>`);
        const gt = src.indexOf(">", i);
        if (gt === -1) throw new JsxError("unterminated closing tag");
        return { js: out.length ? `,${out.join(",")}` : "", end: gt + 1 };
      }
      if (src[i] === "<") {
        flush();
        const child = element(src, i);
        out.push(child.js);
        i = child.end;
        continue;
      }
      if (src[i] === "{") {
        flush();
        const end = balanced(src, i);
        const body = src.slice(i + 1, end - 1);
        // `{/* a comment */}` is a comment, not a child. Anything else is an expression,
        // transformed in turn: `items.map((i) => <Row key={i} />)` is the ordinary way a
        // list is written, and leaving its JSX alone would emit source the browser cannot
        // parse.
        if (body.trim() && !/^\s*\/\*[\s\S]*\*\/\s*$/.test(body)) {
          out.push(`(${transform(body)})`);
        }
        i = end;
        continue;
      }
      text += src[i];
      i += 1;
    }
    throw new JsxError(`<${name}> is never closed`);
  }

  /** Every JSX element in a source, replaced by calls to `H` (and `F` for fragments). */
  function transform(source) {
    let out = "";
    let i = 0;
    while (i < source.length) {
      const c = source[i];
      if (c === "/" && source[i + 1] === "/") {
        const end = source.indexOf("\n", i);
        out += source.slice(i, end === -1 ? source.length : end);
        i = end === -1 ? source.length : end;
        continue;
      }
      if (c === "/" && source[i + 1] === "*") {
        const end = source.indexOf("*/", i + 2);
        if (end === -1) throw new JsxError("unterminated comment");
        out += source.slice(i, end + 2);
        i = end + 2;
        continue;
      }
      if (c === '"' || c === "'" || c === "`") {
        const end = skipString(source, i);
        out += source.slice(i, end);
        i = end;
        continue;
      }
      if (c === "<" && startsTag(source, i)) {
        const el = element(source, i);
        out += el.js;
        i = el.end;
        continue;
      }
      out += c;
      i += 1;
    }
    return out;
  }

  global.ChiefJSX = { transform, JsxError };
})(typeof globalThis === "undefined" ? this : globalThis);
