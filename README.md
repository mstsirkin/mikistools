A collection of small scripts for development

## sashiko-review

Query the [sashiko.dev](https://sashiko.dev) patch review API.

Usage:

    sashiko-review [-m|--markdown] <message-id>
    sashiko-review [-m|--markdown] < message.mbox

Without `-m`/`--markdown`, prints the raw JSON response.
With `-m`/`--markdown`, formats the review as readable markdown.

When run without a message-id argument, reads an email in mbox format from
stdin and extracts the Message-ID automatically.

Requires: `curl`, `jq`
