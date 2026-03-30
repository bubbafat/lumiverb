import { StrictMode } from "react";
import type { ReactElement } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, Navigate, Route, Routes, useLocation } from "react-router-dom";
import AppShell from "./components/AppShell";
import AdminPage from "./pages/AdminPage";
import AdminUsersPage from "./pages/AdminUsersPage";
import BrowsePage from "./pages/BrowsePage";
import CollectionsPage from "./pages/CollectionsPage";
import UnifiedBrowsePage from "./pages/UnifiedBrowsePage";
import CollectionDetailPage from "./pages/CollectionDetailPage";
import PublicCollectionPage from "./pages/PublicCollectionPage";
import ForgotPasswordPage from "./pages/ForgotPasswordPage";
import LibrariesPage from "./pages/LibrariesPage";
import LibrarySettingsPage from "./pages/LibrarySettingsPage";
import LoginPage from "./pages/LoginPage";
import ResetPasswordPage from "./pages/ResetPasswordPage";
import PeoplePage from "./pages/PeoplePage";
import PersonDetailPage from "./pages/PersonDetailPage";
import SettingsPage from "./pages/SettingsPage";
import AccountSection from "./pages/settings/AccountSection";
import PreferencesSection from "./pages/settings/PreferencesSection";
import SecuritySection from "./pages/settings/SecuritySection";
import ApiKeysSection from "./pages/settings/ApiKeysSection";
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
          <Route path="/public/collections/:collectionId" element={<PublicCollectionPage />} />
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
              path="browse"
              element={
                <RequireAuth>
                  {<UnifiedBrowsePage />}
                </RequireAuth>
              }
            />
            <Route
              path="favorites"
              element={<Navigate to="/browse?favorite=true" replace />}
            />
            <Route
              path="collections"
              element={
                <RequireAuth>
                  {<CollectionsPage />}
                </RequireAuth>
              }
            />
            <Route
              path="collections/:collectionId"
              element={
                <RequireAuth>
                  {<CollectionDetailPage />}
                </RequireAuth>
              }
            />
            <Route
              path="people"
              element={
                <RequireAuth>
                  {<PeoplePage />}
                </RequireAuth>
              }
            />
            <Route
              path="people/:personId"
              element={
                <RequireAuth>
                  {<PersonDetailPage />}
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
            <Route
              path="settings"
              element={
                <RequireAuth>
                  {<SettingsPage />}
                </RequireAuth>
              }
            >
              <Route index element={<Navigate to="/settings/account" replace />} />
              <Route path="account" element={<AccountSection />} />
              <Route path="preferences" element={<PreferencesSection />} />
              <Route path="security" element={<SecuritySection />} />
              <Route path="keys" element={<ApiKeysSection />} />
            </Route>
          </Route>
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
);
