import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ApiError,
  forgetYourCertificate,
  getCurrentUser,
  getNetworkAccess,
  httpForOthers,
  keepYourCertificate,
  setNetworkAccess,
  type NetworkAccess,
  type NetworkCertificate,
  type NetworkChange,
} from "../../api/client";

const NETWORK_QUERY_KEY = ["system", "network"];

function said(error: unknown, fallback: string): string {
  return error instanceof ApiError ? error.message : fallback;
}

function untilDate(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

function seenAt(iso: string): string {
  return new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

function Addresses({ addresses }: { addresses: string[] }) {
  return (
    <ul className="space-y-1">
      {addresses.map((address) => (
        <li key={address} className="break-all font-mono text-sm text-indigo-300">{address}</li>
      ))}
    </ul>
  );
}

/** Whether other devices on the network (a phone, another computer) may use a
 * Mac's Lumiverb: over plain HTTP, with nothing to set up on them, and HTTPS
 * too if an admin adds it (the certificate it serves, and plain HTTP turned off
 * once a device has used it). A new install is this Mac only; an admin changes
 * it here, on the Mac. */
export default function NetworkSection() {
  const queryClient = useQueryClient();
  const { data: user } = useQuery({ queryKey: ["settings", "me"], queryFn: getCurrentUser });
  const { data: network, isLoading } = useQuery({ queryKey: NETWORK_QUERY_KEY, queryFn: getNetworkAccess });
  const saved = (next: NetworkAccess) => queryClient.setQueryData(NETWORK_QUERY_KEY, next);
  const change = useMutation({ mutationFn: (next: NetworkChange) => setNetworkAccess(next), onSuccess: saved });

  if (isLoading || !network) {
    return <div className="h-32 rounded-lg border border-gray-700/50 bg-gray-900/50 animate-pulse" />;
  }
  const isAdmin = user?.role === "admin";
  const plain = httpForOthers(network);

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
                onChange={(e) => change.mutate({ other_devices: e.target.checked })}
              />
              <span>
                Let other devices on my network use Lumiverb
                <span className="block text-xs text-gray-500">
                  Phones, tablets and other computers on the same Wi-Fi or network, with nothing to set up on them.
                  Off: this Mac only.
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
            <p className="text-sm text-red-300">{said(change.error, "Couldn't change it. Try again.")}</p>
          )}

          {network.other_devices && plain && (
            <div className="space-y-2">
              <p className="text-sm text-gray-300">On another device, open:</p>
              <Addresses addresses={network.addresses} />
              <p className="text-xs text-gray-500">
                If the Mac asks whether nginx may accept incoming network connections, choose Allow.
              </p>
            </div>
          )}

          {network.other_devices && (
            <Https network={network} isAdmin={isAdmin} pending={change.isPending} onChange={change.mutate}
                   onSaved={saved} />
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

/** HTTPS, which an admin adds if they want it: plain HTTP stays on beside it
 * until they turn it off, once a device has used HTTPS. */
function Https({
  network,
  isAdmin,
  pending,
  onChange,
  onSaved,
}: {
  network: NetworkAccess;
  isAdmin: boolean;
  pending: boolean;
  onChange: (next: NetworkChange) => void;
  onSaved: (next: NetworkAccess) => void;
}) {
  if (!isAdmin && !network.https) return null;
  return (
    <div className="space-y-4 border-t border-gray-700/50 pt-4">
      {isAdmin ? (
        <label className="flex items-start gap-3 text-sm text-gray-200">
          <input
            type="checkbox"
            className="mt-0.5"
            checked={network.https}
            disabled={pending}
            onChange={(e) => onChange({ https: e.target.checked })}
          />
          <span>
            Also offer an encrypted connection (HTTPS)
            <span className="block text-xs text-gray-500">
              Optional. Each device that uses it trusts Lumiverb's certificate once; plain HTTP keeps working
              meanwhile.
            </span>
          </span>
        </label>
      ) : (
        <p className="text-sm text-gray-200">Other devices can use an encrypted connection (HTTPS) too.</p>
      )}

      {network.https && (
        <>
          <div className="space-y-2">
            <p className="text-sm text-gray-300">Over HTTPS, open:</p>
            <Addresses addresses={network.https_addresses} />
          </div>
          {network.certificate && <Certificate certificate={network.certificate} isAdmin={isAdmin} onSaved={onSaved} />}
          <p className="text-xs text-gray-400" role="status">
            {network.https_seen
              ? `A device (${network.https_seen.address}) opened Lumiverb over HTTPS on ${seenAt(network.https_seen.at)}.`
              : "No device has opened Lumiverb over HTTPS yet."}
          </p>
          {isAdmin && (
            <label className="flex items-start gap-3 text-sm text-gray-200">
              <input
                type="checkbox"
                className="mt-0.5"
                checked={network.plain_http}
                // Turned off only once a device has used HTTPS; back on whenever.
                disabled={pending || (network.plain_http && !network.https_seen)}
                onChange={(e) => onChange({ plain_http: e.target.checked })}
              />
              <span>
                Keep plain HTTP for other devices too
                <span className="block text-xs text-gray-500">
                  {network.plain_http && !network.https_seen
                    ? "You can turn it off once a device has opened Lumiverb over HTTPS."
                    : "Off: other devices are sent to HTTPS. This Mac keeps its own address either way."}
                </span>
              </span>
            </label>
          )}
        </>
      )}
    </div>
  );
}

/** Which certificate other devices are served: Lumiverb's own, which each
 * device trusts once (how, here), or one an admin gave (for a public address). */
function Certificate({
  certificate,
  isAdmin,
  onSaved,
}: {
  certificate: NetworkCertificate;
  isAdmin: boolean;
  onSaved: (next: NetworkAccess) => void;
}) {
  const [giving, setGiving] = useState(false);
  const [pem, setPem] = useState("");
  const [key, setKey] = useState("");
  const keep = useMutation({
    mutationFn: () => keepYourCertificate(pem, key),
    onSuccess: (next) => {
      onSaved(next);
      setGiving(false);
      setPem("");
      setKey("");
    },
  });
  const forget = useMutation({ mutationFn: forgetYourCertificate, onSuccess: onSaved });
  const names = certificate.names.join(", ");

  return (
    <div className="space-y-3 rounded-md border border-gray-700/50 p-4">
      {certificate.source === "lumiverb" ? (
        <>
          <p className="text-sm text-gray-200">Each device trusts Lumiverb once</p>
          <p className="text-xs text-gray-400">
            Until it does, its browser says the connection isn't private. On the device, open:
          </p>
          <ul className="space-y-1">
            {certificate.ca_addresses.map((address) => (
              <li key={address} className="break-all font-mono text-xs text-indigo-300">{address}</li>
            ))}
          </ul>
          <ul className="list-disc space-y-1 pl-5 text-xs text-gray-400">
            <li>
              iPhone or iPad: open it in Safari and allow the download. Then in Settings › General › VPN &amp; Device
              Management, install it, and turn it on in Settings › General › About › Certificate Trust Settings.
            </li>
            <li>Mac: open it, then in Keychain Access set it to Always Trust.</li>
          </ul>
          {certificate.ca_fingerprint && (
            <p className="text-xs text-gray-500">
              Its SHA-256 fingerprint, to compare with the one the device shows:{" "}
              <span className="break-all font-mono text-gray-400">{certificate.ca_fingerprint}</span>
            </p>
          )}
          {certificate.ca_url && (
            <p className="text-xs text-gray-400">
              On this Mac:{" "}
              <a href={certificate.ca_url} className="text-indigo-300 underline" download="Lumiverb.crt">
                Download it here
              </a>
            </p>
          )}
        </>
      ) : (
        <p className="text-sm text-gray-200">
          Your certificate, for {names}, until {untilDate(certificate.expires_at)}.
        </p>
      )}

      {isAdmin && certificate.source === "yours" && (
        <button
          type="button"
          className="block text-sm text-indigo-300 hover:text-indigo-200 disabled:opacity-50"
          disabled={forget.isPending}
          onClick={() => forget.mutate()}
        >
          Use Lumiverb's own certificate instead
        </button>
      )}
      {forget.isError && <p className="text-sm text-red-300">{said(forget.error, "Couldn't change it. Try again.")}</p>}

      {isAdmin && certificate.source === "lumiverb" && !giving && (
        <button
          type="button"
          className="block text-sm text-indigo-300 hover:text-indigo-200"
          onClick={() => setGiving(true)}
        >
          Use your own certificate
        </button>
      )}
      {isAdmin && giving && (
        <form
          className="space-y-2"
          onSubmit={(e) => {
            e.preventDefault();
            keep.mutate();
          }}
        >
          <p className="text-xs text-gray-400">
            For an address other devices already trust, from any certificate authority. In PEM form: the certificate
            first, then its chain; the key without a passphrase.
          </p>
          <label className="block text-xs text-gray-300">
            Certificate
            <textarea
              className="mt-1 block h-24 w-full rounded border border-gray-700 bg-gray-950 p-2 font-mono text-xs text-gray-200"
              value={pem}
              onChange={(e) => setPem(e.target.value)}
              placeholder="-----BEGIN CERTIFICATE-----"
            />
          </label>
          <label className="block text-xs text-gray-300">
            Private key
            <textarea
              className="mt-1 block h-24 w-full rounded border border-gray-700 bg-gray-950 p-2 font-mono text-xs text-gray-200"
              value={key}
              onChange={(e) => setKey(e.target.value)}
              placeholder="-----BEGIN PRIVATE KEY-----"
            />
          </label>
          {keep.isError && <p className="text-sm text-red-300">{said(keep.error, "Couldn't keep it. Try again.")}</p>}
          <div className="flex gap-3">
            <button
              type="submit"
              className="rounded bg-indigo-600 px-3 py-1.5 text-sm text-white hover:bg-indigo-500 disabled:opacity-50"
              disabled={keep.isPending || !pem.trim() || !key.trim()}
            >
              Use it
            </button>
            <button type="button" className="text-sm text-gray-400 hover:text-gray-200" onClick={() => setGiving(false)}>
              Cancel
            </button>
          </div>
        </form>
      )}
    </div>
  );
}
