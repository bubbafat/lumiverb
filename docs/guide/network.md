# Network, phones and HTTPS

Lumiverb is a web app: a phone, a tablet or another computer opens it in its browser. This page covers letting other devices in on a Mac, the ports, adding HTTPS, and how a Linux server differs.

## A Mac: this Mac only at first

A new install on a Mac answers only on that Mac, at `http://localhost:6464`. That's on purpose: you set it up there first, before anyone else can reach it.

To let other devices on your network use it, a server admin, on the Mac itself:

1. Open **Settings › Network**.
2. Tick **Let other devices on my network use Lumiverb**.
3. If the Mac asks whether nginx may accept incoming network connections, choose **Allow**.

The page then lists the addresses to open on another device, such as `http://studio.local:6464` and `http://192.168.1.20:6464`: the Mac's name first, then its address on the network. Apple devices usually open the name; some Android phones and Windows PCs need the number. Nothing needs setting up on the device. Untick the box to go back to this Mac only.

The change restarts Lumiverb's web server, so pages may not load for a few seconds. It can be made only on the Mac: elsewhere you get "Change this on the computer Lumiverb is installed on." Everyone signed in can see what's set.

Invite links use these addresses too, so set this before inviting people who'll use another device. See [Users and roles](users-and-roles.md).

## Phones and tablets

Open one of the addresses in the phone's browser and sign in. On a narrow screen, the sidebar becomes a bar at the bottom. The Mac has to be awake and on the same network.

## Ports

On a Mac, one port carries the web app and everything that talks to Lumiverb (the command line, devices, other apps):

| Connection | Port |
|---|---|
| Plain HTTP | 6464 |
| HTTPS, once added | 6465 |

Change them only if something else on the Mac needs one. A server admin, on the Mac: in **Settings › Network**, under **Ports**, type new numbers and click **Change ports**. A port that can't be used (taken by something else, or the same for both) is refused, saying why. The page then gives the Mac's new address, which works within a few seconds: sign in again there. Anything else that uses the old address, such as a device or the command line's `--api-url`, needs the new one.

## HTTPS

Plain HTTP is fine on a home network you trust. To encrypt the connection as well, a server admin, on the Mac:

1. In **Settings › Network**, tick **Also offer an encrypted connection (HTTPS)**.
2. The page lists the HTTPS addresses, on port 6465.

Plain HTTP keeps working beside it, so nothing breaks while devices move over.

### Trusting Lumiverb's certificate

Lumiverb makes its own certificate authority for this Mac and issues the certificate from it. Until a device trusts that authority, its browser says the connection isn't private. Each device trusts it once. On the device, open the address the page gives, ending in `/lumiverb-ca.crt`, then:

- **iPhone or iPad**: open it in Safari and allow the download. In Settings › General › VPN & Device Management, install it. Then turn it on in Settings › General › About › Certificate Trust Settings.
- **Mac**: open it, then in Keychain Access set it to Always Trust. On the Mac Lumiverb runs on, **Download it here** gets it.
- **Other devices**: install it as a trusted certificate authority in the device's own settings.

The page shows the authority's SHA-256 fingerprint: compare it with the one the device shows before trusting it. The certificate is issued again when the Mac's name or address changes, and a month before it ends, without devices needing to trust anything again.

### Your own certificate

If you have a certificate for an address your devices already trust (from any certificate authority), click **Use your own certificate**. Paste the certificate in PEM form, its chain after it, and the private key without a passphrase, then click **Use it**. It's checked first and refused, saying why, if it can't be used. **Use Lumiverb's own certificate instead** goes back.

### Turning plain HTTP off

Once a device has opened Lumiverb over HTTPS, the page says which one and when. Only then can you untick **Keep plain HTTP for other devices too**. Other devices coming over plain HTTP are then sent to HTTPS; the certificate download stays on plain HTTP, so a new device can still get it. The Mac itself keeps its own plain address, `http://localhost:6464`, either way. Turning HTTPS off brings plain HTTP back for everyone.

## Don't put a tunnel on the Mac itself

Some things can only be done on the Mac itself: choosing library folders, moving previews, changing the network, and, on a Mac, Forgot password going straight to a new password. A tunnel or proxy running on the Mac (such as `tailscale serve`, cloudflared or ngrok) can bring other people's requests in as if they came from the Mac. Through one, anyone who reaches it could do those things, including resetting a password. Don't run one on the Lumiverb Mac. To use Lumiverb away from home, connect your device to your home network, for example with a VPN to it.

## A Linux server

On Linux, the install sets up nginx itself, and Settings › Network says who can reach it is set up where it's installed.

- Installed with a domain (`--domain` and `--email`), nginx gets a certificate from Let's Encrypt for that domain and sends plain HTTP to HTTPS. Phones and computers open `https://` and the domain. Unless installed with `--no-firewall`, the firewall lets in SSH, HTTP and HTTPS only.
- On a home network without a domain, it answers over plain HTTP.

See [Install on a Linux server](install-linux.md).
