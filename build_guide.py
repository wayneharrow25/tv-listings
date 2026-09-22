"""Build guide.xml.gz for TiviMate from mapping.csv.

Each <channel id> is the IPTV provider's own epg_channel_id, so TiviMate
matches channels automatically. Channels whose provider id is blank, a junk
placeholder, or already used by a different channel get an extra entry keyed
by their exact provider channel name instead.

If a guide source fails to download (or looks broken), the programmes that
source supplied are carried over from the previous guide.xml.gz rather than
publishing an empty guide.
"""
import collections, copy, csv, datetime as dt, gzip, io, os, sys, time, urllib.request
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
MAPPING = os.path.join(HERE, "mapping.csv")
OUTPUT = os.path.join(HERE, "guide.xml.gz")

SOURCES = {
    "freeview": "https://raw.githubusercontent.com/dp247/Freeview-EPG/master/epg.xml",
    "epgshare": "https://epgshare01.online/epgshare01/epg_ripper_UK1.xml.gz",
}
MIN_PROGRAMMES = 2000            # fewer than this from a source = treat it as failed
PLACEHOLDER_IDS = {"TS"}         # junk ids the provider stamps on unrelated channels
RANK = {"high": 3, "medium": 2, "low": 1}
KEEP_PAST = dt.timedelta(hours=12)


def log(msg):
    print(msg, flush=True)


def fetch(url):
    last = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 guide-builder"})
            with urllib.request.urlopen(req, timeout=180) as r:
                data = r.read()
            return gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data
        except Exception as e:  # network errors, HTTP errors, bad gzip
            last = e
            time.sleep(10 * (attempt + 1))
    raise last


def parse_time(s):
    return dt.datetime.strptime(s.strip()[:20], "%Y%m%d%H%M%S %z") if " " in s.strip() \
        else dt.datetime.strptime(s.strip()[:14], "%Y%m%d%H%M%S").replace(tzinfo=dt.timezone.utc)


def shift(s, hours):
    t = parse_time(s) + dt.timedelta(hours=hours)
    return t.strftime("%Y%m%d%H%M%S %z")


def load_mapping():
    rows = []
    with open(MAPPING, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["guide channel id"].strip() and r["confidence"] in RANK:
                rows.append(r)
    return rows


def plan_channels(rows):
    """Return {output_id: {"names": [...], "source": s, "gid": guide id, "shift": h}}."""
    groups = collections.defaultdict(list)
    for r in rows:
        pid = r["provider epg_channel_id"].strip()
        if pid and pid not in PLACEHOLDER_IDS:
            groups[pid].append(r)

    out, flagged = {}, []

    def target(r):
        gid, _, sh = r["guide channel id"].partition("|")
        return r["guide source"], gid, 1 if sh == "+1h" else 0

    by_name = []
    for pid, rs in groups.items():
        votes = collections.Counter()
        for r in rs:
            votes[r["guide channel id"]] += RANK[r["confidence"]]
        canon = votes.most_common(1)[0][0]
        winner = next(r for r in rs if r["guide channel id"] == canon)
        src, gid, sh = target(winner)
        names = []
        for r in rs:
            if r["guide channel id"] == canon:
                if r["channel name"] not in names:
                    names.append(r["channel name"])
            else:
                by_name.append(r)
                flagged.append(("clash", pid, r["channel name"]))
        out[pid] = {"names": names, "source": src, "gid": gid, "shift": sh}

    for r in rows:
        pid = r["provider epg_channel_id"].strip()
        if not pid or pid in PLACEHOLDER_IDS:
            by_name.append(r)
            flagged.append(("blank" if not pid else "placeholder", pid, r["channel name"]))

    for r in by_name:
        name = r["channel name"]
        if name in out:
            continue
        src, gid, sh = target(r)
        out[name] = {"names": [name], "source": src, "gid": gid, "shift": sh}
    return out, flagged


def read_source(name, url, wanted):
    """Programmes and channel icons for the wanted guide ids from one source."""
    raw = fetch(url)
    progs, icons, total = collections.defaultdict(list), {}, 0
    for _, el in ET.iterparse(io.BytesIO(raw)):
        if el.tag == "channel":
            cid = el.get("id")
            icon = el.find("icon")
            if cid in wanted and icon is not None:
                icons[cid] = icon.get("src")
            el.clear()
        elif el.tag == "programme":
            total += 1
            if el.get("channel") in wanted:
                progs[el.get("channel")].append(copy.deepcopy(el))
            el.clear()
    if total < MIN_PROGRAMMES:
        raise ValueError(f"only {total} programmes")
    return progs, icons


def read_previous():
    progs, icons = collections.defaultdict(list), {}
    if not os.path.exists(OUTPUT):
        return progs, icons
    with gzip.open(OUTPUT, "rb") as f:
        for _, el in ET.iterparse(f):
            if el.tag == "channel":
                icon = el.find("icon")
                if icon is not None:
                    icons[el.get("id")] = icon.get("src")
                el.clear()
            elif el.tag == "programme":
                progs[el.get("channel")].append(copy.deepcopy(el))
                el.clear()
    return progs, icons


def main():
    rows = load_mapping()
    channels, flagged = plan_channels(rows)
    now = dt.datetime.now(dt.timezone.utc)

    fresh, failed = {}, []
    for src, url in SOURCES.items():
        wanted = {c["gid"] for c in channels.values() if c["source"] == src}
        if not wanted:
            continue
        try:
            fresh[src] = read_source(src, url, wanted)
            log(f"{src}: ok, {sum(len(v) for v in fresh[src][0].values())} programmes for {len(wanted)} channels")
        except Exception as e:
            failed.append(src)
            log(f"{src}: FAILED ({e}) - keeping previous data for its channels")
    previous = read_previous() if failed else ({}, {})

    tv = ET.Element("tv", {"generator-info-name": "custom-uk-guide"})
    programmes, total, empty = [], 0, 0
    for oid in sorted(channels):
        c = channels[oid]
        ch = ET.SubElement(tv, "channel", {"id": oid})
        for n in c["names"]:
            ET.SubElement(ch, "display-name").text = n
        if c["source"] in fresh:
            src_progs, src_icons = fresh[c["source"]]
            icon = src_icons.get(c["gid"])
            items = []
            for p in src_progs.get(c["gid"], []):
                q = copy.deepcopy(p)
                q.set("channel", oid)
                if c["shift"]:
                    q.set("start", shift(q.get("start"), c["shift"]))
                    if q.get("stop"):
                        q.set("stop", shift(q.get("stop"), c["shift"]))
                items.append(q)
        else:
            icon = previous[1].get(oid)
            items = list(previous[0].get(oid, []))
        if icon:
            ET.SubElement(ch, "icon", {"src": icon})
        items = [p for p in items if parse_time(p.get("stop") or p.get("start")) > now - KEEP_PAST]
        items.sort(key=lambda p: parse_time(p.get("start")))
        if not items:
            empty += 1
        programmes.extend(items)
        total += len(items)
    tv.extend(programmes)

    if total == 0:
        log("No programmes at all - leaving the existing guide untouched.")
        sys.exit(1)

    ET.indent(tv, space="  ")
    body = b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(tv, encoding="utf-8")
    with open(OUTPUT, "wb") as f, gzip.GzipFile(fileobj=f, mode="wb", mtime=0, filename="guide.xml") as gz:
        gz.write(body)

    kinds = collections.Counter(k for k, _, _ in flagged)
    log(f"wrote {OUTPUT}: {len(channels)} channels, {total} programmes, {empty} channels with no listings")
    log(f"name-keyed entries: {kinds['blank']} blank ids, {kinds['placeholder']} placeholder ids, "
        f"{kinds['clash']} clashing ids")
    if failed:
        log(f"sources carried over from the previous guide: {', '.join(failed)}")


if __name__ == "__main__":
    main()
