/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // Sortie autonome : image Docker minimale (server.js + dépendances nécessaires).
  output: "standalone",
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
