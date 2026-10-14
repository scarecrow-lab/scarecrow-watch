<?xml version="1.0" encoding="UTF-8"?>
<xsl:stylesheet version="1.0"
  xmlns:xsl="http://www.w3.org/1999/XSL/Transform"
  xmlns:atom="http://www.w3.org/2005/Atom">
<xsl:output method="html" encoding="UTF-8" indent="yes" doctype-system="about:legacy-compat"/>
<xsl:template match="/">
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title><xsl:value-of select="rss/channel/title"/></title>
<style>
:root{ --bg:#14181f; --panel:#1c222c; --panel-2:#232a35; --line:#333d4b; --ink:#e9e6df; --ink-dim:#a9b0bc; --ink-faint:#6f7889; --gold:#c99a4b; --crit:#d98a6a; }
@media (prefers-color-scheme: light){ :root{ --bg:#f2efe8; --panel:#fff; --panel-2:#faf8f3; --line:#ddd6c6; --ink:#20242b; --ink-dim:#4d5560; --ink-faint:#8a8f98; --gold:#a87a2f; --crit:#b85c3a; } }
*{ box-sizing:border-box; }
body{ margin:0; background:var(--bg); color:var(--ink); font-family:'IBM Plex Sans',-apple-system,'Segoe UI',system-ui,sans-serif; line-height:1.5; }
.wrap{ max-width:760px; margin:0 auto; padding:34px 22px 60px; }
.badge{ display:inline-block; font:700 10.5px ui-monospace,monospace; letter-spacing:.12em; color:var(--gold); border:1px solid var(--gold); border-radius:999px; padding:3px 10px; margin-bottom:12px; }
h1{ font-size:28px; margin:0 0 6px; line-height:1.15; }
.lede{ color:var(--ink-dim); margin:0 0 18px; font-size:14.5px; }
.howto{ background:var(--panel-2); border:1px solid var(--line); border-left:4px solid var(--gold); border-radius:12px; padding:14px 16px; font-size:13.5px; color:var(--ink-dim); margin-bottom:26px; }
.howto b{ color:var(--ink); }
.howto code{ font:500 12px ui-monospace,monospace; color:var(--ink); background:rgba(201,154,75,.14); padding:2px 7px; border-radius:6px; word-break:break-all; }
a{ color:var(--gold); }
.item{ border-top:1px solid var(--line); padding:16px 0; }
.item .meta{ font:500 11.5px ui-monospace,monospace; color:var(--ink-faint); margin-bottom:5px; }
.item .ititle{ font-size:16px; font-weight:600; color:var(--ink); text-decoration:none; display:inline-block; }
.item .ititle:hover{ color:var(--gold); }
.item .desc{ margin:5px 0 0; color:var(--ink-dim); font-size:13.5px; }
.foot{ margin-top:28px; padding-top:18px; border-top:1px solid var(--line); color:var(--ink-faint); font-size:12.5px; }
</style>
</head>
<body>
<div class="wrap">
  <div class="badge">RSS FEED</div>
  <h1><xsl:value-of select="rss/channel/title"/></h1>
  <p class="lede"><xsl:value-of select="rss/channel/description"/></p>
  <div class="howto">
    <p style="margin:0 0 8px"><b>This is a live feed, not a normal web page.</b> Copy this page's address and paste it into any RSS reader (Feedly, Inoreader, NetNewsWire, Thunderbird…) to get new alerts automatically — no account or email needed.</p>
    <p style="margin:0">Prefer to just read them? <a href="alerts.html">Open the Alerts page →</a></p>
  </div>
  <xsl:for-each select="rss/channel/item">
    <div class="item">
      <div class="meta"><xsl:value-of select="pubDate"/></div>
      <a class="ititle"><xsl:attribute name="href"><xsl:value-of select="link"/></xsl:attribute><xsl:value-of select="title"/></a>
      <p class="desc"><xsl:value-of select="description"/></p>
    </div>
  </xsl:for-each>
  <p class="foot"><a href="index.html">Scarecrow Watch dashboard</a> · <a href="alerts.html">All alerts</a></p>
</div>
</body>
</html>
</xsl:template>
</xsl:stylesheet>
