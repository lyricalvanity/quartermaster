# Security

Quartermaster runs as the person who owns the machine and can start and stop that person's processes. Its
posture (README, "What it will not do", and SPEC.md section 2) is the contract:

- both ports bind loopback; reach beyond the machine is a deliberate edit and a reverse proxy with its own login;
- the agent executes only what `inventory.json` says; a request supplies an op and an id, never an argument;
- the page can write presets and nothing else;
- nothing leaves the machine unless someone clicks a check or an update.

The bearer key keeps a browser honest, not a hostile local user: any process running as the owner can read
it. Treat a Quartermaster host as a single-user machine.

To report a problem with any of the above, open a private security advisory on the repository rather than a
public issue, and say which rule you believe is broken and how.
