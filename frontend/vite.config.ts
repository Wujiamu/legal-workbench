import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

const devHost = '127.0.0.1:5173';
const apiHost = '127.0.0.1:8765';

export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    cors: false,
    proxy: {
      '/api': {
        target: `http://${apiHost}`,
        bypass(req) {
          if (req.headers.host === devHost) {
            req.headers.host = apiHost;
            if (req.headers.origin === `http://${devHost}`) {
              req.headers.origin = `http://${apiHost}`;
            }
          }
        },
      },
    },
  },
});
