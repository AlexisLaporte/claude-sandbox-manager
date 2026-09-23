const net = require("net"), fs = require("fs"), https = require("https");
const port = p => new Promise(r => {
  const s = net.connect({ host: "127.0.0.1", port: p, timeout: 1500 });
  s.on("connect", () => { r(`${p} OUVERT`); s.destroy(); });
  s.on("error", () => r(`${p} bloqué`));
  s.on("timeout", () => { r(`${p} bloqué`); s.destroy(); });
});
(async () => {
  for (const p of [5432, 8099, 3000, 8190]) console.log(await port(p));
  for (const f of ["/opt/deploy", "/etc/ferme", "/srv/ferme/bacs"]) {
    try { fs.readdirSync(f); console.log(f, "lisible"); } catch (e) { console.log(f, "refusé", e.code); }
  }
  fs.writeFileSync(`${process.env.HOME}/ok`, "1"); console.log("home écrivable");
  https.get("https://api.anthropic.com", r => { console.log("api.anthropic.com joignable", r.statusCode); process.exit(0); })
    .on("error", e => { console.log("anthropic KO", e.code); process.exit(1); });
})();
