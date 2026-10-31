(async () => {
  const UA = { 'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)' };
  for (const repo of ['iBotPeaches/Apktool']) {
    const r = await fetch(`https://github.com/${repo}/releases/latest`, { headers: UA, redirect: 'follow' });
    const t = await r.text();
    const tags = [...new Set([...t.matchAll(/releases\/tag\/([^"']+)/g)].map(m => m[1]))];
    console.log(repo, 'latest tag:', tags[0]);
    const e = await fetch(`https://github.com/${repo}/releases/expanded_assets/${tags[0]}`, { headers: UA });
    const h = await e.text();
    const assets = [...new Set([...h.matchAll(/href="([^"]*releases\/download\/[^"]+)"/g)].map(m => m[1]))];
    assets.forEach(a => console.log('  ', a));
  }
})();
