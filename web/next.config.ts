import type { NextConfig } from "next";

const API_URL = process.env.FINRECUR_API_URL ?? "http://localhost:8000";

const nextConfig: NextConfig = {
  // The backend doesn't send CORS headers (out of scope for this build -- backend/ isn't
  // touched here), so browser fetches to it fail cross-origin in local dev. Proxying
  // through Next's own origin sidesteps that without changing the API. Set
  // NEXT_PUBLIC_API_URL=/api to route through this rewrite.
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${API_URL}/:path*` }];
  },
};

export default nextConfig;
