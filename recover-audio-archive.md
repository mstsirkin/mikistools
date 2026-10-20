# Recover audio from damaged ZIP archives

`recover_audio_archive.py` salvages individual audio members that pass both the
original ZIP CRC and a complete FFmpeg decode. It leaves the source unchanged
and does not rebuild the ZIP or import tracks into a catalog.

## Requirements and usage

Requires Linux, Python 3, bubblewrap (`bwrap`), FFmpeg, and permission to create
unprivileged namespaces. The sandbox expects `/usr/bin/python3`, FFmpeg on
`/usr/bin:/bin`, a merged `/usr` layout, and `/etc/ld.so.cache`. There is no
unsandboxed fallback. The destination needs at least 7 GiB free at startup.

```sh
python3 recover_audio_archive.py damaged.zip --output ./recovered
```

Results go into `recovered/<archive-sha256>/`. Audio files have generated names
such as `000000.mp3`; `report.json` maps them to original member names and records
sizes, SHA256 hashes, validation results, and rejection reasons. Keep the report
with the audio. Existing result directories are never overwritten.

A successful exit means inspection finished, not that every member was recovered.
Inspect `recovered`, `rejected`, member statuses, and any `error` in the report.
An unreadable ZIP can produce a report with no recovered files.
`archive_complete` deliberately remains false: this is salvage, not certification
of a complete archive. RAR and 7z archives are not supported.

Supported audio extensions: MP3, WAV, FLAC, M4A, OGG, OPUS, AAC, APE, WMA, AIF,
and AIFF. Unrelated members are skipped.

## Uniform offset recovery and baseline

Some damaged ZIPs have directory offsets that point a constant distance beyond
all actual local headers. The recovery variant can subtract a uniform positive
offset of at most 1 MiB in memory, only when the file starts with a local header,
all original header positions are invalid, and every corrected position is
unique and contains the local-header signature. Python's ZIP reader still checks
member names and overlaps; CRC and full audio decoding are still mandatory.
The original bytes and CRC values are never changed.

`recover_audio_archive_strict.py` preserves the baseline without this offset
correction. Compare it using a separate output directory:

```sh
python3 recover_audio_archive_strict.py damaged.zip --output ./strict-results
python3 recover_audio_archive.py damaged.zip --output ./offset-results
```

The motivating archive had a uniform 1,283-byte offset error. Correcting offsets
in memory recovered all 12 audio members with their original CRCs and successful
full decodes. This establishes the offset inconsistency, not which program
created it. Other forms of corruption may remain unrecoverable.

## Security boundaries and limits

Both variants parse and decode a snapshot inside a mandatory offline bubblewrap
sandbox. The sandbox has no home directory access, mounts the snapshot read-only,
and can write only to private staging output and temporary storage. Host `/usr`
is mounted read-only for dependencies. No archive member is executed, and archive
paths are never used as output paths. Absolute paths, traversal, drive prefixes,
control characters, symbolic links, special files, and encrypted members are
rejected. FFmpeg uses a fixed demuxer and only the `file,pipe` protocols.

Limits include 2 GiB input, 2,000 members, 256 MiB per member, 2 GiB total declared
expanded data, and a 200:1 compression ratio. Resource limits cap address space at
2 GiB and CPU time at 300 seconds; the parent imposes a 360-second wall timeout,
and each decode has a 60-second timeout. These conservative limits can reject
legitimate archives. A failed sandbox run promotes no output.

Before promotion, the parent checks output names, file types, link counts, sizes,
and hashes against the report. This is defense in depth using the host kernel,
Python, bubblewrap, and FFmpeg; it is not a guarantee against vulnerabilities in
those components. Review reports before importing salvaged tracks elsewhere.

## Tests

```sh
python3 -m unittest discover -s tests -p test_recover_audio_archive.py -v
```

The tests exercise full sandboxed decoding, CRC failures, unsafe paths, symlinks,
encrypted entries, expansion limits, unreadable archives, source preservation,
repeat-output refusal, and synthetic uniform offset corruption.
