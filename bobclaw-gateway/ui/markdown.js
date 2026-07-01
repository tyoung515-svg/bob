import { h } from "preact";

let keyIndex = 0;

function nextKey(prefix) {
  keyIndex += 1;
  return `${prefix}-${keyIndex}`;
}

function safeHref(href) {
  try {
    const url = new URL(href, location.href);
    if (url.protocol === "http:" || url.protocol === "https:" || url.protocol === "mailto:") {
      return url.href;
    }
  } catch (_error) {
    return null;
  }

  return null;
}

function inlineNodes(text) {
  const nodes = [];
  const tokenPattern = /(`[^`]+`|\*\*[^*]+\*\*|\*[^*\n]+\*|\[[^\]\n]+\]\([^) \n]+\))/g;
  let lastIndex = 0;
  let match = tokenPattern.exec(text);

  while (match) {
    if (match.index > lastIndex) {
      nodes.push(text.slice(lastIndex, match.index));
    }

    const token = match[0];

    if (token.startsWith("`")) {
      nodes.push(h("code", { key: nextKey("code") }, token.slice(1, -1)));
    } else if (token.startsWith("**")) {
      nodes.push(h("strong", { key: nextKey("strong") }, inlineNodes(token.slice(2, -2))));
    } else if (token.startsWith("*")) {
      nodes.push(h("em", { key: nextKey("em") }, inlineNodes(token.slice(1, -1))));
    } else {
      const linkMatch = token.match(/^\[([^\]]+)\]\(([^)]+)\)$/);
      const href = linkMatch ? safeHref(linkMatch[2]) : null;
      if (href) {
        nodes.push(
          h(
            "a",
            {
              key: nextKey("link"),
              href,
              target: "_blank",
              rel: "noreferrer"
            },
            inlineNodes(linkMatch[1])
          )
        );
      } else {
        nodes.push(token);
      }
    }

    lastIndex = match.index + token.length;
    match = tokenPattern.exec(text);
  }

  if (lastIndex < text.length) {
    nodes.push(text.slice(lastIndex));
  }

  return nodes;
}

function flushParagraph(blocks, lines) {
  if (!lines.length) {
    return;
  }

  blocks.push(h("p", { key: nextKey("p") }, inlineNodes(lines.join(" "))));
  lines.length = 0;
}

function flushList(blocks, listItems, ordered) {
  if (!listItems.length) {
    return;
  }

  const tag = ordered ? "ol" : "ul";
  blocks.push(
    h(
      tag,
      { key: nextKey(tag) },
      listItems.map((item) => h("li", { key: nextKey("li") }, inlineNodes(item)))
    )
  );
  listItems.length = 0;
}

export function Markdown({ text }) {
  const source = text || "";
  const lines = source.replace(/\r\n/g, "\n").split("\n");
  const blocks = [];
  const paragraph = [];
  const listItems = [];
  let listOrdered = false;
  let inCodeBlock = false;
  let codeLines = [];

  for (const line of lines) {
    if (line.trim().startsWith("```")) {
      flushParagraph(blocks, paragraph);
      flushList(blocks, listItems, listOrdered);

      if (inCodeBlock) {
        blocks.push(
          h(
            "pre",
            { key: nextKey("pre") },
            h("code", null, codeLines.join("\n"))
          )
        );
        codeLines = [];
        inCodeBlock = false;
      } else {
        inCodeBlock = true;
      }
      continue;
    }

    if (inCodeBlock) {
      codeLines.push(line);
      continue;
    }

    if (!line.trim()) {
      flushParagraph(blocks, paragraph);
      flushList(blocks, listItems, listOrdered);
      continue;
    }

    const unorderedMatch = line.match(/^\s*[-*]\s+(.+)$/);
    const orderedMatch = line.match(/^\s*\d+\.\s+(.+)$/);

    if (unorderedMatch || orderedMatch) {
      flushParagraph(blocks, paragraph);
      const nextOrdered = Boolean(orderedMatch);
      if (listItems.length && nextOrdered !== listOrdered) {
        flushList(blocks, listItems, listOrdered);
      }
      listOrdered = nextOrdered;
      listItems.push(unorderedMatch ? unorderedMatch[1] : orderedMatch[1]);
      continue;
    }

    flushList(blocks, listItems, listOrdered);
    paragraph.push(line.trim());
  }

  if (inCodeBlock) {
    blocks.push(
      h(
        "pre",
        { key: nextKey("pre") },
        h("code", null, codeLines.join("\n"))
      )
    );
  }

  flushParagraph(blocks, paragraph);
  flushList(blocks, listItems, listOrdered);

  return h("div", { class: "markdown-body" }, blocks.length ? blocks : h("p", null, ""));
}
