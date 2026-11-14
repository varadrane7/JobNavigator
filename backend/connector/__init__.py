"""The connector: JobNavigator as a remote MCP server.

One endpoint (`POST /mcp`, Streamable HTTP, stateless) gives any MCP client a small set of
tools over one account's data: claude.ai / Claude Desktop / mobile (a custom connector,
signed in with OAuth), Claude Code and other agents (a bearer token from Settings).

    tokens.py   signed connector tokens, the per-account revocation counter
    tools.py    the tools: what a model may read and change
    oauth.py    the OAuth 2.1 authorization server claude.ai needs (PKCE, dynamic clients)
    routes.py   /mcp itself and the Settings API (/api/connector)

Nothing is stored for OAuth: clients, codes and tokens are HMAC-signed values (the key that
signs the account cookie), so there is no table to migrate and nothing to clean up.
"""
