import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import "./index.css";

// The API is on localhost: it is reachable without internet, so never pause queries for "offline".
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      networkMode: "always",
      retry: (count, err) => count < 2 && !("status" in err && err.status === 401),
    },
    mutations: { networkMode: "always" },
  },
});

const root = document.getElementById("root");
if (!root) throw new Error("#root missing");

createRoot(root).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>
  </StrictMode>,
);
