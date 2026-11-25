-- Macros temporaires (durée de la session de transformation uniquement).

-- Identifiant d'opérateur commun aux deux sources :
-- Arcep écrit "Bouygues Telecom" / "Free", l'ANFR "BOUYGUES TELECOM" / "FREE MOBILE".
create or replace temp macro norm_operateur(nom) as
    case upper(trim(nom))
        when 'ORANGE' then 'ORANGE'
        when 'SFR' then 'SFR'
        when 'BOUYGUES TELECOM' then 'BOUYGUES'
        when 'FREE' then 'FREE'
        when 'FREE MOBILE' then 'FREE'
        else trim(regexp_replace(upper(strip_accents(trim(nom))), '[^A-Z0-9]+', '_', 'g'), '_')
    end;

-- Code département sur 2 caractères (3 pour l'outre-mer) :
-- l'ANFR écrit "001", "02A", "974" ; l'Arcep "01 " avec un espace final.
create or replace temp macro norm_departement(code) as
    case
        when code is null or trim(code) = '' then null
        when length(trim(code)) = 3 and starts_with(trim(code), '0') then substr(trim(code), 2)
        else upper(trim(code))
    end;

-- Etat d'un service : 'HS' -> true, 'OK' -> false, absent -> null (techno non déployée sur le site).
create or replace temp macro est_hs(etat) as
    case upper(trim(etat)) when 'HS' then true when 'OK' then false else null end;
