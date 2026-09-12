/**
 * Next.js configuration.
 *
 * `output: "standalone"` is required, not cosmetic: infra/docker/Dockerfile.frontend
 * builds a slim runner stage with
 *
 *     COPY --from=builder /app/.next/standalone ./
 *     CMD ["node", "server.js"]
 *
 * Next only emits `.next/standalone` (and the `server.js` entrypoint that
 * command runs) when this option is set. Without it the directory never exists,
 * the COPY fails, and the image — and therefore `docker compose up frontend` —
 * cannot build.
 *
 * @type {import('next').NextConfig}
 */
const nextConfig = {
  output: "standalone",
};

export default nextConfig;
