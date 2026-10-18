# Standalone Pi session → HTML

`tools/export_session.py` turns one Pi JSONL session into one self-contained HTML
page. Copy the script anywhere: it needs Python 3.11+ and the standard library,
not Pi, a server, a build step, JavaScript packages, or an internet connection
for ordinary/cached exports. Optional AI chapter generation uses live Ollama Cloud
inference and existing subscription/usage credits.
The synthetic example below is safe to use for a preview.

```sh
python3 tools/export_session.py tests/fixtures/session-export.jsonl -o /tmp/pi-session-demo.html
# Open /tmp/pi-session-demo.html in a browser.

python3 tools/export_session.py /path/to/session.jsonl -o /path/to/session.html
python3 tools/export_session.py /path/to/session.jsonl --title 'A useful conversation'
python3 tools/export_session.py /path/to/session.jsonl --leaf ENTRY_ID
python3 tools/export_session.py /path/to/session.jsonl --all-branches
python3 tools/export_session.py /path/to/session.jsonl --no-tools
python3 tools/export_session.py /path/to/session.jsonl --include-thinking --include-system
```

Without `-o`, the destination is the input path with an `.html` suffix. Existing
outputs are refused unless `--force` is specified. The input is never modified;
input/output aliases, including hard links, are rejected. Files are created with
owner-only permissions (`0600` on Unix); `--force` atomically replaces the output.
If the output is a symlink to another file, replacement replaces the link, not its
target. Missing destination directories are not created automatically.

## Reading experience

The visual direction follows the Sprites field guide: charcoal and plum panels,
cream text, gold serif headings, sage and lavender accents, subtle iridescent
edges, and a left navigation rail. No artwork, web fonts, stylesheets, or scripts
are downloaded or embedded from the reference site.

- User-turn navigation and permanent entry anchors.
- Search across exported text, including collapsed details, with role filtering.
- Collapsible tool arguments, tool results, summaries, and optional thinking.
- Responsive layout, visible keyboard focus, reduced-motion support, and print CSS.
- Server-rendered transcript: it remains readable without JavaScript; search and
  the expand/collapse-all button require JavaScript.
- A safe Markdown subset: headings, paragraphs, bold, inline code, fenced code,
  flat lists, blockquotes, and HTTP(S)/mailto/fragment links. Raw HTML is escaped;
  tables, nested lists, emphasis, and complex CommonMark syntax stay plain text.
  Tool output is verbatim, not Markdown. Images never load, including Markdown images.

## AI chapters with DeepSeek Flash

`--chapters` replaces turn-by-turn sidebar links with topic chapters. Each chapter
also appears as a heading and short summary **in the transcript scroll**, separate
from the original messages. Sidebar links jump to these headings. Search and role
filters hide chapters with no matching messages; following a chapter link clears
the filters. The generated text is labeled as an AI summary, not original dialogue.

```sh
# First run: uses live Ollama Cloud inference. Supply your own key privately.
# If Pi is installed, its credential helper can provide your configured key:
OLLAMA_API_KEY="$(pi auth print-api-key --provider ollama-cloud)" \
  python3 tools/export_session.py /path/to/session.jsonl -o /path/to/session.html --chapters

# Repeat export: reads the private cache, needs no key and makes no model call.
python3 tools/export_session.py /path/to/session.jsonl -o /path/to/session.html --chapters --force
```

The model is `deepseek-v4.1-flash` at `https://ollama.com/api/chat`; no local model
or offline fallback is used to generate chapter summaries. The exporter does not
buy credits, top up balances, or change account settings. Authorize credit usage
before running it. Requests use at most 80 KB of JSON and cap output at 4,096
tokens, with thinking disabled. One request groups all user turns; long messages
are excerpted from their beginning and end to fit the bound. If even minimal
excerpts cannot fit, the export fails instead of silently losing the tail or
submitting an unbounded series of calls.

Only user text and assistant text blocks are sent. Tool arguments/results,
direct shell executions, thinking, images, system prompts, summaries and extension
state are excluded. Light regex redaction removes common credentials, bearer/basic
auth, recognizable token formats, emails, URLs, IPv4 addresses, common absolute
paths, and local/internal hostnames. It is **not comprehensive anonymization**:
ordinary prose, names, relative paths, quoted output in prose, and unrecognized
secret formats can remain. Redaction applies to the inference input and generated
chapter text, **not to the original transcript rendered in the HTML**.

The first request saves these owner-only files beside the HTML:

- `session.chapters.json.request.json`: the exact redacted model request.
- `session.chapters.json`: validated headings, summaries, reported usage and an
  input fingerprint. The original conversation is not duplicated in this cache.

Use `--chapter-cache /private/path/chapters.json` to choose another cache path.
A matching cache is reused without inference; a mismatching/invalid cache fails
rather than triggering an unexpected charge. To deliberately generate a fresh
plan, choose a new cache path. There are no automatic retries, including after a
timeout: the request may already have consumed credits. Its retained request file
blocks an accidental repeat at the same path. Inspect the provider outcome before
explicitly choosing another path. Cached chapter starts must begin at turn 1,
strictly increase, and reference real user turns. AI output is escaped like all
other transcript text. Chaptering supports one branch, not `--all-branches`.

## Which branch?

Pi v2/v3 JSONL is a tree, not necessarily one chronological conversation. By
default, the exporter follows `parentId` from the **last recorded entry** back to
its root. `--leaf` chooses an explicit endpoint. `--all-branches` instead shows
all entries in file order and annotates entry/parent IDs; it is a history view,
not a single conversation. The file does not reliably record a later UI-only
navigation to an older leaf, so the exporter does not claim to know that position.
Legacy v1 linear sessions are supported. Missing/duplicate IDs and parents that
do not refer to earlier records fail with an error instead of silently dropping
history. Malformed JSON fails without echoing the offending line. For a session
still being written, export a stable copy; a partial trailing record is an error.

This is a **raw historical transcript**, not reconstructed model context:
compaction does not erase earlier messages; context-edit events are noted without
rewriting the original message. Model-change and summary events are rendered.
Internal extension state and other metadata entries are skipped. The three
counters describe rendered user messages, assistant messages, and tool results;
reported per-message tokens are not whole-session accounting totals.

## Privacy and safety

Ordinary/cached export uploads nothing. With `--chapters`, the first generation
sends only the bounded, redacted excerpts described above to Ollama Cloud.
HTML is escaped, remote content cannot load, and a content
security policy permits only the bundled, hash-pinned script and inline styles.
Links require a deliberate click. There is no raw session JSON hidden in the page.

By default, the output **omits** thinking, system messages/sections, provider
signatures, image payloads, hidden extension messages, and internal extension
state. `--include-thinking` and `--include-system` opt into sensitive text;
compaction system checkpoints and opaque signatures are always omitted.
Unknown content blocks get an omission marker, not a raw dump.

Tool calls and results are included by default, only **collapsed**, not redacted.
`--no-tools` removes those payloads but does **not** remove direct shell execution
messages, summaries, or references to tool output elsewhere in the conversation.
Summaries, ordinary text, titles, and tool output can contain secrets or personal
details. This is **not an anonymizer**: review the generated file before sharing.
Private permissions do not make its contents safe to publish.

## Tests

```sh
python3 -m unittest discover -s tests -p test_export_session.py -v
python3 -m ruff check --select E4,E7,E9,F tools/export_session.py tests/test_export_session.py
```

The exporter has no dependencies. The test suite additionally uses Node to execute
the exact embedded search/filter/detail script against a small synthetic DOM.
Tests cover branching, legacy records, malformed input, private omissions, HTML
injection, safe links, output permissions, overwrite protection, and CLI behavior.
Automated tests use synthetic transcripts and mocked provider responses, not real
sessions or provider calls. Chapter tests cover redaction, inference-payload
exclusions, request bounds, chapter validation, private cache reuse without a key,
no automatic retries, and chapter headings in both navigation and transcript.
