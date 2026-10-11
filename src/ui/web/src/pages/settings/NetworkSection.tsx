import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, getCurrentUser, getNetworkAccess, setNetworkAccess } from "../../api/client";

const NETWORK_QUERY_KEY = ["system", "network"];

/** Whether other devices on the network (a phone, another computer) may use a
 * Mac's Lumiverb. A new install is this Mac only; an admin changes it here, on the Mac. */
export default function NetworkSection() {
  const queryClient = useQueryClient();
  const { data: user } = useQuery({ queryKey: ["settings", "me"], queryFn: getCurrentUser });
  const { data: network, isLoading } = useQuery({ queryKey: NETWORK_QUERY_KEY, queryFn: getNetworkAccess });
  const change = useMutation({
    mutationFn: setNetworkAccess,
    onSuccess: (next) => queryClient.setQueryData(NETWORK_QUERY_KEY, next),
  });

  if (isLoading || !network) {
    return <div className="h-32 rounded-lg border border-gray-700/50 bg-gray-900/50 animate-pulse" />;
  }
  const isAdmin = user?.role === "admin";

  return (
    <div className="rounded-lg border border-gray-700/50 bg-gray-900/50 p-6 space-y-5">
      <div>
        <h2 className="text-lg font-semibold text-gray-100">Network</h2>
        <p className="mt-1 text-sm text-gray-400">Which devices can open Lumiverb.</p>
      </div>

      {!network.available ? (
        <p className="text-sm text-gray-300">
          Lumiverb doesn't run this server's web server, so who can reach it is set up where it's installed.
        </p>
      ) : (
        <>
          {isAdmin ? (
            <label className="flex items-start gap-3 text-sm text-gray-200">
              <input
                type="checkbox"
                className="mt-0.5"
                checked={network.other_devices}
                disabled={change.isPending}
                onChange={(e) => change.mutate(e.target.checked)}
              />
              <span>
                Let other devices on my network use Lumiverb
                <span className="block text-xs text-gray-500">
                  Phones, tablets and other computers on the same Wi-Fi or network. Off: this Mac only.
                  Turn it on only on a network you trust: the connection isn't encrypted.
                </span>
              </span>
            </label>
          ) : (
            <div className="space-y-1">
              <p className="text-sm text-gray-200">
                {network.other_devices
                  ? "Other devices on your network can use Lumiverb."
                  : "Only the Mac Lumiverb is installed on can use it."}
              </p>
              <p className="text-sm text-gray-500">Only admins can change this.</p>
            </div>
          )}

          {change.isError && (
            <p className="text-sm text-red-300">
              {change.error instanceof ApiError ? change.error.message : "Couldn't change it. Try again."}
            </p>
          )}

          {network.other_devices && (
            <div className="space-y-2">
              <p className="text-sm text-gray-300">On another device, open:</p>
              <ul className="space-y-1">
                {network.addresses.map((address) => (
                  <li key={address} className="break-all font-mono text-sm text-indigo-300">{address}</li>
                ))}
              </ul>
              <p className="text-xs text-gray-500">
                If the Mac asks whether nginx may accept incoming network connections, choose Allow.
              </p>
            </div>
          )}

          {isAdmin && (
            <p className="text-xs text-gray-500">
              Changing this restarts Lumiverb's web server: pages may not load for a few seconds.
            </p>
          )}
        </>
      )}
    </div>
  );
}
