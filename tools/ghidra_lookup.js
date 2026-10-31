(async () => {
  const r = await fetch('https://github.com/NationalSecurityAgency/ghidra/releases/latest', {
    headers: { 'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)' },
    redirect: 'follow',
  });
  const t = await r.text();
  console.log('status', r.status, 'final', r.url, 'bytes', t.length);
  const tags = [...new Set([...t.matchAll(/releases\/tag\/([^"']+)/g)].map(m => m[1]))];
  console.log('tags', tags.slice(0, 5));
  const assets = [...new Set([...t.matchAll(/href="([^"]*releases\/download\/[^"]+)"/g)].map(m => m[1]))];
  console.log('assets:');
  assets.slice(0, 12).forEach(a => console.log('  ', a));
})();
