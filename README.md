A collection of small scripts for development

## nordproxy

Run a command through a random NordVPN SOCKS5 proxy:

    ./nordproxy curl https://example.com

Reads `username:password` from `~/.nordvpn-username:password.txt`
and randomly selects a `server:port` line from `~/.nordproxies.txt`.
Sets `http_proxy` and `https_proxy`, then replaces itself with the command,
preserving its arguments. The command must support these proxy variables.

Requires: Bash, `sed`, `shuf`

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
