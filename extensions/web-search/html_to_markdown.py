#!/usr/bin/env python3
"""Extract article-like text from downloaded HTML; no network access."""

# /// script
# requires-python = ">=3.10"
# dependencies = ["beautifulsoup4==4.14.2"]
# ///
import re
import sys
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup, NavigableString, Tag

DROP = re.compile(r"(^|[-_ ])(nav|menu|cookie|advert|ads?|social|sidebar|footer|breadcrumb|popup|share|subscribe|related)([-_ ]|$)", re.I)


def inline(node, base=""):
    if isinstance(node, NavigableString):
        return re.sub(r"\s+", " ", str(node))
    if not isinstance(node, Tag):
        return ""
    if node.name == "img":
        src = node.get("src") or node.get("data-src")
        url = urljoin(base, src or "")
        if not src or urlparse(url).scheme not in ("http", "https"):
            return ""
        alt = (node.get("alt") or "image").replace("]", "\\]")
        return f"![{alt}]({url})"
    text = "".join(inline(child, base) for child in node.children)
    if node.name in ("strong", "b"):
        return f"**{text.strip()}**"
    if node.name in ("em", "i"):
        return f"*{text.strip()}*"
    if node.name == "code":
        return f"`{text.strip()}`"
    if node.name == "a" and node.get("href", "").startswith(("http://", "https://")):
        return f"[{text.strip()}]({node['href']})" if text.strip() else ""
    if node.name == "br":
        return "\n"
    return text


def blocks(node, base=""):
    out = []
    for child in node.children:
        if not isinstance(child, Tag):
            continue
        name = child.name
        if name in ("h1", "h2", "h3", "h4", "h5", "h6"):
            out.append("#" * int(name[1]) + " " + inline(child, base).strip())
        elif name == "p":
            out.append(inline(child, base).strip())
        elif name in ("ul", "ol"):
            lines = []
            for i, li in enumerate(child.find_all("li", recursive=False), 1):
                label = f"{i}." if name == "ol" else "-"
                lines.append(f"{label} {inline(li, base).strip()}")
            out.append("\n".join(lines))
        elif name == "table":
            rows = [[inline(cell, base).strip().replace("|", "\\|") for cell in tr.find_all(("th", "td"), recursive=False)] for tr in child.find_all("tr")]
            rows = [row for row in rows if row]
            if rows:
                width = max(map(len, rows))
                padded = [row + [""] * (width - len(row)) for row in rows]
                lines = ["| " + " | ".join(row) + " |" for row in padded]
                lines.insert(1, "| " + " | ".join(["---"] * width) + " |")
                out.append("\n".join(lines))
        elif name == "img":
            out.append(inline(child, base))
        elif name == "pre":
            out.append("```\n" + child.get_text().strip() + "\n```")
        elif name == "blockquote":
            out.append("\n".join("> " + line for line in child.get_text(" ", strip=True).splitlines()))
        else:
            out.extend(blocks(child, base))
    return [entry for entry in out if entry.strip()]


def convert(html, base=""):
    soup = BeautifulSoup(html, "html.parser")
    root = soup.find("main") or soup.find("article") or soup.body or soup
    heading = root.find("h1")
    title = (heading.get_text(" ", strip=True) if heading else soup.title.get_text(" ", strip=True) if soup.title else "").strip()
    for element in list(root.find_all(True)):
        if not element.name or element.attrs is None:
            continue
        marker = " ".join([element.get("id", ""), *element.get("class", [])])
        if element.name in ("script", "style", "nav", "footer", "header", "aside", "form", "button", "svg", "iframe", "noscript") or DROP.search(marker):
            element.decompose()
    result = blocks(root, base)
    if title and not any(line.lstrip("# ") == title for line in result[:2]):
        result.insert(0, "# " + title)
    return "\n\n".join(result).strip()


if __name__ == "__main__":
    print(convert(sys.stdin.read(), sys.argv[1] if len(sys.argv) > 1 else ""))
