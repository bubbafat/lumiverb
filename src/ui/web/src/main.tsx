import { StrictMode } from "react";
import type { ReactElement } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, Navigate, Route, Routes, useLocation } from "react-router-dom";
import AppShell from "./components/AppShell";
import AdminPage from "./pages/AdminPage";
import AdminUsersPage from "./pages/AdminUsersPage";
import BrowsePage from "./pages/BrowsePage";
import ForgotPasswordPage from "./pages/ForgotPasswordPage";
import LibrariesPage from "./pages/LibrariesPage";
import LibrarySettingsPage from "./pages/LibrarySettingsPage";
import LoginPage from "./pages/LoginPage";
import ResetPasswordPage from "./pages/ResetPasswordPage";
import { getApiKey } from "./api/client";
import "./index.css";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      staleTime: 30_000,
    },
  },
});

function RequireAuth({ children }: { children: ReactElement }) {
  const location = useLocation();
  const apiKey = getApiKey();
  if (!apiKey) {
    return (
      <Navigate
        to={`/login?next=${encodeURIComponent(
          `${location.pathname}${location.search}`,
        )}`}
        replace
      />
    );
  }
  return children;
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route path="/forgot-password" element={<ForgotPasswordPage />} />
          <Route path="/reset-password" element={<ResetPasswordPage />} />
          <Route path="/" element={<AppShell />}>
            <Route path="libraries/:libraryId/browse" element={<BrowsePage />} />
            <Route
              index
              element={<RequireAuth>{<LibrariesPage />}</RequireAuth>}
            />
            <Route
              path="libraries/:libraryId/settings"
              element={
                <RequireAuth>
                  {<LibrarySettingsPage />}
                </RequireAuth>
              }
            />
            <Route
              path="admin"
              element={
                <RequireAuth>
                  {<AdminPage />}
                </RequireAuth>
              }
            />
            <Route
              path="admin/users"
              element={
                <RequireAuth>
                  {<AdminUsersPage />}
                </RequireAuth>
              }
            />
          </Route>
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
);
