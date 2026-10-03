/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // Sortie autonome : image Docker minimale (server.js + dépendances nécessaires).
  output: "standalone",
  // La page d'activation porte dans son URL une demande K-access signée (`?req=`) : elle ne doit
  // fuir vers aucun site tiers par l'en-tête Referer.
  async headers() {
    return [{ source: "/activation", headers: [{ key: "Referrer-Policy", value: "no-referrer" }] }];
  },
  async redirects() {
    return [
      // « Carburant » est devenu « Gestion de l'énergie » (la flotte comporte aussi des
      // véhicules électriques). Redirection permanente : les liens et favoris existants
      // continuent de fonctionner.
      { source: "/fuel", destination: "/energie", permanent: true },
    ];
  },
};

export default nextConfig;
