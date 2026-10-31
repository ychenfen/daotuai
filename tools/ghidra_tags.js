(async () => {
  const UA = { 'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)' };
  const r = await fetch('https://github.com/NationalSecurityAgency/ghidra/tags', { headers: UA });
  const t = await r.text();
  const tags = [...new Set([...t.matchAll(/releases\/tag\/([A-Za-z0-9_.\-]+)"/g)].map(m => m[1]))];
  console.log('recent tags:', tags.slice(0, 12));

  for (const tag of tags.slice(0, 4)) {
    const a = await fetch(`https://github.com/NationalSecurityAgency/ghidra/releases/expanded_assets/${tag}`, { headers: UA });
    const h = await a.text();
    const assets = [...new Set([...h.matchAll(/href="([^"]*releases\/download\/[^"]+)"/g)].map(m => m[1]))];
    console.log(`\n[${tag}] assets:`);
    assets.forEach(x => console.log('  ', x));
  }
})();
