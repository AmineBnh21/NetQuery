-- Sites indisponibles (Arcep), typés et nettoyés.
-- Grain : un site déclaré hors service (au moins partiellement) un jour donné.
-- Le fichier quotidien est un instantané : un même site apparaît chaque jour tant qu'il est HS.

create or replace table stg_sites_indispo as
with typed as (
    select
        date                                               as date_releve,
        norm_operateur(operateur)                          as operateur_id,
        nullif(trim(station_anfr), '')                     as station_anfr,
        nullif(trim(code_insee), '')                       as code_insee,
        norm_departement(departement)                      as departement,
        nullif(trim(commune), '')                          as commune,
        try_cast(lat as double)                            as lat,
        try_cast(lon as double)                            as lon,
        -- état par service et par technologie (null = techno absente du site)
        est_hs(voix2g) as voix_2g_hs,
        est_hs(voix3g) as voix_3g_hs,
        est_hs(voix4g) as voix_4g_hs,
        est_hs(data3g) as data_3g_hs,
        est_hs(data4g) as data_4g_hs,
        est_hs(data5g) as data_5g_hs,
        -- état global du service
        est_hs(voix)   as voix_hs,
        est_hs(data)   as data_hs,
        case upper(trim(raison)) when 'INT' then 'incident' when 'MAINT' then 'maintenance' end as raison,
        nullif(trim(detail), '')                           as detail,
        -- "propre" : indicateur 0/1 fourni par l'Arcep (signification à confirmer dans sa documentation)
        try_cast(propre as integer) = 1                    as propre,
        try_strptime(debut, '%Y-%m-%d %H:%M:%S')           as debut,
        try_cast(fin as date)                              as fin_prevue,
        try_strptime(debut_voix, '%Y-%m-%d %H:%M:%S')      as debut_voix,
        try_cast(fin_voix as date)                         as fin_voix_prevue,
        try_strptime(debut_data, '%Y-%m-%d %H:%M:%S')      as debut_data,
        try_cast(fin_data as date)                         as fin_data_prevue
    from raw_sites_indispo
),
keyed as (
    select
        *,
        -- Clé du site : numéro de station ANFR, sinon identifiant synthétique
        -- (certaines lignes Orange n'ont pas de station ANFR).
        coalesce(
            station_anfr,
            'NA-' || operateur_id || '-' || left(md5(concat_ws('|', code_insee, lat, lon)), 10)
        ) as site_id
    from typed
)
select *
from keyed
-- quelques doublons (même site, même jour) : on garde la déclaration la plus récente
qualify row_number() over (
    partition by date_releve, site_id
    order by debut desc nulls last, fin_prevue desc nulls last
) = 1;
