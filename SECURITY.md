# Beveiligingsbeleid

## Een kwetsbaarheid melden

Heb je een kwetsbaarheid gevonden in Inleverboks? Meld die dan **niet** via een openbaar issue.
Gebruik **GitHub Private Vulnerability Reporting**: ga in de repository naar
*Security* → *Report a vulnerability*.

Vermeld in je melding:

- een beschrijving van het probleem en de mogelijke impact;
- de stappen om het te reproduceren (eventueel een proof of concept);
- de versie of commit waarop je het gevonden hebt.

We bevestigen de ontvangst binnen 5 werkdagen en houden je op de hoogte van de oplossing.
Publiceer de details pas nadat er een gepatchte versie beschikbaar is, of na 90 dagen in
overleg.

## Ondersteunde versies

Alleen de nieuwste release en de `main`-branch krijgen beveiligingsupdates.

## Buiten scope

- Aanvallen waarvoor je fysieke toegang tot de server of het datavolume nodig hebt.
- Bevindingen die alleen optreden zonder TLS of met een verkeerd ingestelde `TRUSTED_PROXIES`.
- Rate limiting dat bij meerdere replicas per pod geldt. Dat is bekend en gedocumenteerd.
