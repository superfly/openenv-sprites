# Security policy

## Supported versions

This project is experimental. Security fixes are applied to the latest code on
`main` and, after publication begins, the latest released package. Older package
versions may require upgrading to receive a fix.

## Report a vulnerability

Do not open a public issue, discussion, or pull request for an undisclosed
vulnerability. Email [security@fly.io](mailto:security@fly.io) with:

- the affected component and version or commit;
- the security impact and expected attack scenario;
- reproduction steps or a minimal proof of concept;
- any conditions required to exploit the issue; and
- a suggested mitigation, if you have one.

Please remove Sprites tokens, environment credentials, personal data, and other
unrelated secrets from the report. The Fly.io security team can provide a Signal
number if sensitive information requires another transport.

## Research guidelines

When investigating a potential issue:

- use Sprites, accounts, and data you control;
- avoid service disruption, denial of service, and data destruction;
- do not access, retain, or disclose another party's secrets or data;
- stop testing and report the issue if you encounter sensitive information; and
- allow a reasonable opportunity to remediate before public disclosure.
