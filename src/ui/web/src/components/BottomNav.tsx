import { NavLink, useParams } from "react-router-dom";

function HomeIcon() {
  return (
    <svg className="h-5 w-5" viewBox="0 0 24 24" fill="none" aria-hidden>
      <path
        d="M3 12L12 3l9 9M5 10v9a1 1 0 001 1h4v-5h4v5h4a1 1 0 001-1v-9"
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function GridIcon() {
  return (
    <svg className="h-5 w-5" viewBox="0 0 24 24" fill="none" aria-hidden>
      <rect x="3" y="3" width="7" height="7" rx="1.5" stroke="currentColor" strokeWidth="1.7" />
      <rect x="14" y="3" width="7" height="7" rx="1.5" stroke="currentColor" strokeWidth="1.7" />
      <rect x="3" y="14" width="7" height="7" rx="1.5" stroke="currentColor" strokeWidth="1.7" />
      <rect x="14" y="14" width="7" height="7" rx="1.5" stroke="currentColor" strokeWidth="1.7" />
    </svg>
  );
}

function GearIcon() {
  return (
    <svg className="h-5 w-5" viewBox="0 0 24 24" fill="none" aria-hidden>
      <path
        d="M10.325 4.317a1.5 1.5 0 013.35 0l.143.955a1.5 1.5 0 002.104 1.128l.88-.439a1.5 1.5 0 012.012.683l.75 1.5a1.5 1.5 0 01-.683 2.012l-.88.44a1.5 1.5 0 00-1.128 2.103l.439.88a1.5 1.5 0 01-.683 2.012l-1.5.75a1.5 1.5 0 01-2.012-.683l-.44-.88a1.5 1.5 0 00-2.103-1.128l-.88.439a1.5 1.5 0 01-2.012-.683l-.75-1.5a1.5 1.5 0 01.683-2.012l.88-.44a1.5 1.5 0 001.128-2.103l-.439-.88a1.5 1.5 0 01.683-2.012z"
        stroke="currentColor"
        strokeWidth="1.3"
      />
      <circle cx="12" cy="12" r="2.5" stroke="currentColor" strokeWidth="1.3" />
    </svg>
  );
}

function PeopleIcon() {
  return (
    <svg className="h-5 w-5" viewBox="0 0 24 24" fill="none" aria-hidden>
      <path
        d="M15 19.128a9.38 9.38 0 002.625.372 9.337 9.337 0 004.121-.952 4.125 4.125 0 00-7.533-2.493M15 19.128v-.003c0-1.113-.285-2.16-.786-3.07M15 19.128v.106A12.318 12.318 0 018.624 21c-2.331 0-4.512-.645-6.374-1.766l-.001-.109a6.375 6.375 0 0111.964-3.07M12 6.375a3.375 3.375 0 11-6.75 0 3.375 3.375 0 016.75 0zm8.25 2.25a2.625 2.625 0 11-5.25 0 2.625 2.625 0 015.25 0z"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function SlidersIcon() {
  return (
    <svg className="h-5 w-5" viewBox="0 0 24 24" fill="none" aria-hidden>
      <path
        d="M6 13.5V3m0 10.5a2.25 2.25 0 000 4.5m0-4.5a2.25 2.25 0 010 4.5M6 21v-3.5m6-14v7.5m0-7.5a2.25 2.25 0 000 4.5m0-4.5a2.25 2.25 0 010 4.5M12 21V14.5m6-11.5v4m0-4a2.25 2.25 0 000 4.5m0-4.5a2.25 2.25 0 010 4.5M18 21V11.5"
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinecap="round"
      />
    </svg>
  );
}

const linkClass = ({ isActive }: { isActive: boolean }) =>
  `flex flex-1 flex-col items-center gap-1 py-3 text-xs transition-colors duration-150 ${
    isActive ? "text-indigo-400" : "text-gray-400"
  }`;

export function BottomNav() {
  const { libraryId } = useParams<{ libraryId?: string }>();

  return (
    // z-40 — above page content; DrawerOverlay uses z-50 so the open drawer covers this bar
    <nav className="fixed bottom-0 inset-x-0 z-40 flex border-t border-gray-800 bg-gray-950 pb-safe md:hidden">
      <NavLink to="/" end className={linkClass}>
        <HomeIcon />
        <span>Libraries</span>
      </NavLink>

      {libraryId && (
        <NavLink to={`/libraries/${libraryId}/browse`} className={linkClass}>
          <GridIcon />
          <span>Browse</span>
        </NavLink>
      )}

      <NavLink to="/people" className={linkClass}>
        <PeopleIcon />
        <span>People</span>
      </NavLink>

      <NavLink to="/admin" className={linkClass}>
        <GearIcon />
        <span>Admin</span>
      </NavLink>

      {libraryId && (
        <NavLink to={`/libraries/${libraryId}/settings`} className={linkClass}>
          <SlidersIcon />
          <span>Settings</span>
        </NavLink>
      )}
    </nav>
  );
}
