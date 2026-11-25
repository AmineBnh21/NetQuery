-- Emetteurs ANFR (observatoire 2G/3G/4G/5G), typés.
-- Grain : un émetteur = une station x un système (ex. "LTE 800").
-- Une station (sta_nm_anfr) appartient à un seul opérateur et est posée sur un support (sup_id, ex. pylône).
-- raw_anfr_stations est une vue créée par transform.py vers l'archive ANFR la plus récente.

create or replace table stg_anfr_emetteurs as
select
    try_cast(id as bigint)                                       as emetteur_id,
    trim(sta_nm_anfr)                                            as station_anfr,
    trim(sup_id)                                                 as support_id,
    norm_operateur(adm_lb_nom)                                   as operateur_id,
    trim(adm_lb_nom)                                             as operateur_libelle_anfr,
    trim(emr_lb_systeme)                                         as systeme,
    split_part(trim(emr_lb_systeme), ' ', 1)                     as norme,          -- GSM, UMTS, LTE, 5G
    try_cast(regexp_extract(emr_lb_systeme, '(\d+)\s*$', 1) as integer) as bande_mhz,
    trim(generation)                                             as techno,         -- 2G, 3G, 4G, 5G
    trim(statut)                                                 as statut,
    -- actif = en service ou techniquement opérationnel (la 5G n'a que ce second statut dans l'ANFR)
    trim(statut) in ('En service', 'Techniquement opérationnel') as est_actif,
    try_cast(emr_dt as date)                                     as date_mise_service,
    trim(code_insee)                                             as code_insee,
    norm_departement(sta_nm_dpt)                                 as departement,
    nullif(trim(adr_nm_cp), '')                                  as code_postal,
    try_cast(trim(split_part(coordonnees, ',', 1)) as double)    as lat,
    try_cast(trim(split_part(coordonnees, ',', 2)) as double)    as lon,
    try_cast(replace(sup_nm_haut, ',', '.') as double)           as hauteur_m,
    nullif(trim(nat_id), '')                                     as nature_support_id,
    nullif(trim(tpo_id), '')                                     as proprietaire_support_id,
    nullif(trim(list_azimut), '')                                as azimuts,
    concat_ws(', ', nullif(trim(adr_lb_lieu), ''), nullif(trim(adr_lb_add1), ''),
              nullif(trim(adr_lb_add2), ''), nullif(trim(adr_lb_add3), '')) as adresse,
    try_cast(date_maj as timestamp)                              as date_maj
from raw_anfr_stations;
