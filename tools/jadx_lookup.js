(async () => {
  const UA = { 'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)' };
  const r = await fetch('https://github.com/1013503897/jadx-headless-mcp/releases', { headers: UA });
  const t = await r.text();
  console.log('status', r.status, 'bytes', t.length);
  const tags = [...new Set([...t.matchAll(/releases\/tag\/([^"']+)/g)].map(m => m[1]))];
  console.log('tags', tags.slice(0, 8));
  const assets = [...new Set([...t.matchAll(/href="([^"]*releases\/download\/[^"]+)"/g)].map(m => m[1]))];
  console.log('assets on page:');
  assets.forEach(a => console.log('  ', a));
  for (const tag of tags.slice(0, 3)) {
    const e = await fetch(`https://github.com/1013503897/jadx-headless-mcp/releases/expanded_assets/${tag}`, { headers: UA });
    const h = await e.text();
    const list = [...new Set([...h.matchAll(/href="([^"]*releases\/download\/[^"]+)"/g)].map(m => m[1]))];
    console.log(`[${tag}]`, list);
  }
})();
