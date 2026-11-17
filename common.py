"""Shared validation and HTML text helpers; no API or third-party dependencies."""

from html.parser import HTMLParser
import os
from pathlib import Path
from typing import Any


class CheckError(Exception):
    """A safe diagnostic without credentials or raw server responses."""


def replace_file(source,target,attempts=5):
    """os.replace with short retries: on Windows a reader (UI poll, bot, antivirus) briefly locks the target."""
    import time
    for attempt in range(attempts):
        try:os.replace(source,target);return
        except PermissionError:
            if attempt==attempts-1:raise
            time.sleep(0.2*(attempt+1))


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
        replace_file(temporary,path)  # Windows: a reader briefly holds the file (audit A20)
    finally:
        if os.path.exists(temporary):os.unlink(temporary)


def daily_backup(db_path,folder,*,today=None,keep=14):
    """One consistent copy of the database per day (sqlite backup API, safe while others read it);
    only the last `keep` daily copies `jobs-YYYY-MM-DD.sqlite` are kept, other files are never touched."""
    import sqlite3
    from datetime import date
    from pathlib import Path
    db_path,folder=Path(db_path),Path(folder)
    if not db_path.exists():return None
    folder.mkdir(parents=True,exist_ok=True)
    target=folder/f'jobs-{today or date.today().isoformat()}.sqlite'
    if not target.exists():
        temporary=target.with_suffix('.tmp')
        source=sqlite3.connect(db_path,timeout=10);copy=sqlite3.connect(temporary)
        try:source.backup(copy)
        finally:copy.close();source.close()
        temporary.replace(target)
    for old in sorted(folder.glob('jobs-????-??-??.sqlite'))[:-keep]:old.unlink()
    return target


def api_error_detail(response,limit=300):
    """Provider's own error text (OpenRouter `error.message`), shortened; never headers or request data."""
    try:message=response.json()['error']['message']
    except Exception:return ''
    return ' '.join(str(message).split())[:limit] if message else ''


def rebase(path,root):
    """A path recorded before the project folder was moved/restored (audit A13): the same file under `root`
    (anchored at its «drafts» or «profile» part), or the path unchanged. Paths already under `root` are kept."""
    p=Path(path);root=Path(root)
    try:
        if p.resolve().is_relative_to(root.resolve()):return p
    except (OSError,ValueError):return p
    parts=p.parts
    for anchor in ('drafts','profile'):
        if anchor in parts:
            index=len(parts)-1-parts[::-1].index(anchor)
            candidate=root.joinpath(*parts[index:])
            if candidate.exists():return candidate
    return p


def http_error_text(prefix,response):
    detail=api_error_detail(response).rstrip('.')
    text=f'{prefix}: HTTP {response.status_code}'+(f' — {detail}' if detail else '')
    if 'max price' in detail.lower():
        text+=('. Ни один провайдер модели не укладывается в лимит цены программы (assess.PRICE_CAP): '
               'провайдеры подняли цены, лимит нужно пересмотреть')
    return text+'.'


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
