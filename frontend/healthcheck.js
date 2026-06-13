// Sonde de santé du conteneur frontend (Next.js standalone).
// Node est garanti présent (runtime). On vérifie qu'une page publique répond 200.
// Évite toute dépendance à busybox wget et tout guillemet imbriqué dans le compose.
fetch("http://127.0.0.1:3000/login")
  .then((r) => process.exit(r.ok ? 0 : 1))
  .catch(() => process.exit(1));
