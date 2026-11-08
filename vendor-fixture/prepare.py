#!/usr/bin/env python3
"""Verify fixed public artifacts and extract selected files; never install RPMs."""

import argparse
import base64
import hashlib
import json
import os
import pathlib
import selectors
import stat
import subprocess
import time
import urllib.parse
import urllib.request


ALLOWED_HOSTS = {"mirror.stream.centos.org", "www.centos.org", "repo.almalinux.org"}
MAX_PAYLOAD = 128 << 20


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        raise RuntimeError("redirected artifact requests are not allowed")


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def command(args, timeout=30):
    result = subprocess.run([str(arg) for arg in args], capture_output=True,
                            timeout=timeout, env={"PATH": "/usr/bin:/usr/sbin:/bin:/sbin", "LC_ALL": "C"})
    require(result.returncode == 0, "verification command failed: " + str(args[0]) + ": "
            + result.stderr.decode(errors="replace")[-1000:])
    return result.stdout


def download(item, directory):
    url = urllib.parse.urlparse(item["url"])
    require(url.scheme == "https" and url.hostname in ALLOWED_HOSTS and not url.username,
            "artifact URL outside fixed public sources")
    require(0 < item["bytes"] <= 32 << 20, "artifact size outside bound")
    opener = urllib.request.build_opener(NoRedirect())
    with opener.open(item["url"], timeout=25) as response:
        final = urllib.parse.urlparse(response.url)
        require(final.scheme == "https" and final.hostname in ALLOWED_HOSTS,
                "artifact redirected outside fixed public sources")
        data = response.read(item["bytes"] + 1)
    require(len(data) == item["bytes"] and digest(data) == item["sha256"],
            "artifact size or digest mismatch")
    name = item["filename"]
    require(pathlib.PurePosixPath(name).name == name and name not in {"", ".", ".."},
            "unsafe artifact filename")
    path = directory / name
    with path.open("xb") as handle:
        handle.write(data)
    return path, data


def primary_fingerprint(armored):
    text = armored.decode("ascii")
    require("-----BEGIN PGP PUBLIC KEY BLOCK-----" in text
            and "-----END PGP PUBLIC KEY BLOCK-----" in text, "not an armored public key")
    body = text.split("-----BEGIN PGP PUBLIC KEY BLOCK-----", 1)[1].split(
        "-----END PGP PUBLIC KEY BLOCK-----", 1)[0]
    encoded = "".join(line.strip() for line in body.splitlines()
                      if line.strip() and ":" not in line and not line.startswith("="))
    packet = base64.b64decode(encoded, validate=True)
    offset, found = 0, []
    while offset < len(packet):
        header = packet[offset]
        offset += 1
        require(header & 0x80, "invalid public-key packet")
        if header & 0x40:
            tag = header & 0x3F
            first = packet[offset]
            offset += 1
            if first < 192:
                length = first
            elif first < 224:
                length = (first - 192) * 256 + packet[offset] + 192
                offset += 1
            else:
                require(first == 255, "partial public-key packet is unsupported")
                length = int.from_bytes(packet[offset:offset + 4], "big")
                offset += 4
        else:
            tag, width = (header >> 2) & 15, header & 3
            require(width != 3, "indeterminate public-key packet is unsupported")
            width = (1, 2, 4)[width]
            length = int.from_bytes(packet[offset:offset + width], "big")
            offset += width
        require(0 <= length <= len(packet) - offset, "truncated public-key packet")
        data = packet[offset:offset + length]
        offset += length
        if tag == 6:
            require(data and data[0] == 4 and len(data) < 65536, "expected a v4 primary public key")
            found.append(hashlib.sha1(b"\x99" + len(data).to_bytes(2, "big") + data).hexdigest().upper())
    require(len(found) == 1, "expected exactly one primary public key")
    return found[0]


def bounded_payload(rpm):
    process = subprocess.Popen(["rpm2cpio", str(rpm)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               env={"PATH": "/usr/bin:/usr/sbin:/bin:/sbin", "LC_ALL": "C"})
    streams = selectors.DefaultSelector()
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    bounds = {"stdout": MAX_PAYLOAD, "stderr": 65536}
    deadline = time.monotonic() + 30
    try:
        for name, pipe in (("stdout", process.stdout), ("stderr", process.stderr)):
            os.set_blocking(pipe.fileno(), False)
            streams.register(pipe, selectors.EVENT_READ, name)
        while streams.get_map():
            remaining = deadline - time.monotonic()
            require(remaining > 0, "RPM payload extraction timed out")
            for key, _ in streams.select(min(remaining, 0.25)):
                block = os.read(key.fileobj.fileno(), min(65536, bounds[key.data] - len(buffers[key.data]) + 1))
                if not block:
                    streams.unregister(key.fileobj)
                    continue
                require(len(buffers[key.data]) + len(block) <= bounds[key.data],
                        "RPM payload output exceeds its bound")
                buffers[key.data].extend(block)
        remaining = deadline - time.monotonic()
        require(remaining > 0 and process.wait(timeout=remaining) == 0, "RPM payload extraction failed")
        return bytes(buffers["stdout"])
    finally:
        streams.close()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        process.stdout.close()
        process.stderr.close()


def verify_selected(item, rpm, destination):
    raw = command(["rpm", "-qp", "--qf", "[%{FILENAMES}|%{FILEDIGESTS}|%{FILELINKTOS}\\n]", rpm]).decode()
    header = {}
    for line in raw.splitlines():
        name, checksum, link = line.split("|", 2)
        header[name.removeprefix("/")] = (checksum, link)
    verified = {}
    for name in item["members"]:
        require(name in header, "selected member missing from signed RPM header")
        checksum, link = header[name]
        path = destination / name
        resolved = path.resolve(strict=True)
        require(destination.resolve() in resolved.parents and resolved.is_file(), "selected path escapes fixture root")
        if link:
            require(path.is_symlink() and os.readlink(path) == link, "selected library link differs from RPM header")
        else:
            require(path.is_file() and not path.is_symlink() and digest(path.read_bytes()) == checksum,
                    "selected file differs from signed RPM header")
        verified[name] = {"sha256": digest(resolved.read_bytes()), "link": link or None}
    return verified


def extract_selected(rpm, destination, selected):
    data = bounded_payload(rpm)
    wanted = set(selected)
    require(len(wanted) == len(selected), "duplicate selected member")
    for name in wanted:
        path = pathlib.PurePosixPath(name)
        require(not path.is_absolute() and ".." not in path.parts and "\\" not in name
                and str(path) == name and name.startswith("usr/"), "unsafe selected member")
    offset, seen, pending_links = 0, set(), []
    while offset + 110 <= len(data):
        header = data[offset:offset + 110]
        require(header[:6] in {b"070701", b"070702"}, "unsupported CPIO payload")
        fields = [int(header[6 + index * 8:14 + index * 8], 16) for index in range(13)]
        mode, links, size, name_size = fields[1], fields[4], fields[6], fields[11]
        require(1 <= name_size <= 4096 and size <= MAX_PAYLOAD, "invalid CPIO member bounds")
        offset += 110
        require(offset + name_size <= len(data), "truncated CPIO name")
        raw_name = data[offset:offset + name_size]
        require(raw_name.endswith(b"\x00"), "unterminated CPIO name")
        name = raw_name[:-1].decode("utf-8")
        offset = (offset + name_size + 3) & ~3
        require(offset + size <= len(data), "truncated CPIO content")
        contents = data[offset:offset + size]
        offset = (offset + size + 3) & ~3
        if name == "TRAILER!!!":
            break
        if name.startswith("./"):
            name = name[2:]
        if name not in wanted:
            continue
        require(name not in seen, "duplicate selected CPIO member")
        seen.add(name)
        target = destination / name
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        require(not target.exists() and not target.is_symlink(), "extraction target already exists")
        if stat.S_ISREG(mode):
            require(links == 1, "hard-linked selected files are unsupported")
            with target.open("xb") as handle:
                handle.write(contents)
            target.chmod(0o755 if mode & 0o111 else 0o644)
        elif stat.S_ISLNK(mode):
            link = contents.decode("utf-8")
            require(pathlib.PurePosixPath(link).name == link and link not in {"", ".", ".."},
                    "selected library link must name a sibling")
            require(str(pathlib.PurePosixPath(name).parent / link) in wanted,
                    "selected link target is not in the reviewed member set")
            pending_links.append((target, link))
        else:
            raise RuntimeError("selected member is not a regular file or library symlink")
    require(seen == wanted, "selected RPM members missing")
    for target, link in pending_links:
        target.symlink_to(link)
    result = {}
    for name in sorted(wanted):
        path = destination / name
        resolved = path.resolve(strict=True)
        require(destination.resolve() in resolved.parents and resolved.is_file(), "selected path escapes fixture root")
        result[name] = {"sha256": digest(resolved.read_bytes()), "link": os.readlink(path) if path.is_symlink() else None}
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock", required=True)
    parser.add_argument("--root", required=True)
    args = parser.parse_args()
    root = pathlib.Path(args.root)
    require(root.is_absolute() and not root.exists() and not root.is_symlink(), "artifact root must be new")
    root.mkdir(mode=0o700)
    lock = json.loads(pathlib.Path(args.lock).read_text())
    downloads, keydb = root / "downloads", root / "keydb"
    downloads.mkdir(mode=0o700)
    keydb.mkdir(mode=0o700)
    command(["rpm", "--dbpath", keydb, "--initdb"])
    keys = {}
    for item in lock["keys"]:
        path, raw = download(item, downloads)
        require(primary_fingerprint(raw) == item["fingerprint"], "public-key fingerprint mismatch")
        command(["rpm", "--dbpath", keydb, "--import", path])
        keys[item["id"]] = item["fingerprint"]
    report = {"scope": "fixed public artifacts extracted into new fixture roots only",
              "package_installation": False, "service_actions": False, "packages": []}
    for item in lock["packages"]:
        rpm, _ = download(item, downloads)
        signature = command(["rpmkeys", "--dbpath", keydb, "--checksig", "--verbose", rpm]).decode()
        require("key id " + keys[item["key"]][-8:].lower() + ": ok" in signature.lower(),
                "signature does not identify the required pinned key")
        metadata = command(["rpm", "-qp", "--qf", "%{NAME}|%{VERSION}|%{RELEASE}|%{ARCH}", rpm]).decode()
        require(metadata == item["metadata"], "RPM metadata differs from the lock")
        require(item["root"] in {"baseline", "candidate", "ovs", "runtime"}, "unknown extraction root")
        destination = root / item["root"]
        destination.mkdir(mode=0o700, exist_ok=True)
        extract_selected(rpm, destination, item["members"])
        files = verify_selected(item, rpm, destination)
        report["packages"].append({"filename": item["filename"], "sha256": item["sha256"],
                                   "metadata": metadata, "signature_key": item["key"],
                                   "root": item["root"], "files": files})
    (root / "verified-artifacts.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"verified_packages": len(report["packages"]), "package_installation": False}))


if __name__ == "__main__":
    main()
