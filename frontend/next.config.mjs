/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // Sortie autonome : image Docker minimale (server.js + dépendances nécessaires).
  output: "standalone",
};

export default nextConfig;
