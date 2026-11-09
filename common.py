"""Shared validation and HTML text helpers; no API or third-party dependencies."""

from html.parser import HTMLParser
from typing import Any


class CheckError(Exception):
    """A safe diagnostic without credentials or raw server responses."""


def write_json(path,value):
    """Replace a report atomically; concurrent readers never see half a JSON file."""
    import json,os,tempfile
    from pathlib import Path
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,temporary=tempfile.mkstemp(prefix=path.name+'.',suffix='.tmp',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as stream:
            json.dump(value,stream,ensure_ascii=False,indent=2);stream.write('\n')
            stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,path)
    finally:
        if os.path.exists(temporary):os.unlink(temporary)


class DescriptionParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in {"script", "style"}:
            self.hidden += 1
        elif not self.hidden and tag in {
            "p", "br", "li", "div", "h1", "h2", "h3", "tr", "ul", "ol"
        }:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        elif not self.hidden and tag in {
            "p", "li", "div", "h1", "h2", "h3", "tr", "ul", "ol"
        }:
            self.parts.append("\n")


def html_to_text(description: str) -> str:
    parser = DescriptionParser()
    parser.feed(description)
    parser.close()
    lines = (" ".join(line.split()) for line in "".join(parser.parts).splitlines())
    return "\n".join(line for line in lines if line)
