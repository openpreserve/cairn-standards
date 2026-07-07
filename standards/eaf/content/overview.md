<!-- Landing-page copy supplied by TS-EAS (Karin Bredenberg), July 2026. -->

An XML standard for encoding the functions performed by agents - the WHY behind the records.

Where **EAD** describes *WHAT* was created and **EAC-CPF** describes *WHO* created it, **EAC-F**
describes *WHY* it was created.

Based on the International Standard for Describing Functions (ISDF), 2007.

## Using the schema

Always track the latest EAC-F v1 release:

```
https://standards.openpreservation.org/eaf/v1/eaf.xsd
```

Or pin an exact version:

```
https://standards.openpreservation.org/eaf/v1.0.0/eaf.xsd
```

The XML namespace for EAC-F v1 is `https://standards.openpreservation.org/eaf/v1`. It stays
constant across minor and patch releases; the precise version is carried in the schema's
`schema-version` attribute. (The public identifier is `eaf`, matching the schema's namespace;
the standard's name is EAC-F.)

## Governance

EAC-F is maintained by the Technical Subcommittee on Encoded Archival Standards (TS-EAS) of the
Society of American Archivists. Development happens in the open on GitHub; this site hosts a
preserved, integrity-checked copy of each release - schema serialisations and the Tag Library.
