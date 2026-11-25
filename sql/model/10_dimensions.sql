-- Dimensions du modèle.

-- ----------------------------------------------------------------- dim_operateur
-- Les 4 opérateurs de métropole + les opérateurs d'outre-mer présents dans l'ANFR.
create or replace table dim_operateur as
with ids as (
    select operateur_id, any_value(operateur_libelle_anfr) as libelle
    from stg_anfr_emetteurs group by all
    union
    select distinct operateur_id, operateur_id from stg_sites_indispo
)
select
    operateur_id,
    case operateur_id
        when 'ORANGE' then 'Orange'
        when 'SFR' then 'SFR'
        when 'BOUYGUES' then 'Bouygues Telecom'
        when 'FREE' then 'Free Mobile'
        else any_value(libelle)
    end as nom,
    operateur_id in ('ORANGE', 'SFR', 'BOUYGUES', 'FREE') as est_metropole
from ids
group by operateur_id;

-- ----------------------------------------------------------------- dim_departement
create or replace table dim_departement as
select departement, nom, region_code, region
from stg_geo_departements
union all
-- collectivités d'outre-mer présentes dans les données mais absentes du référentiel
select distinct e.departement, null, null, null
from stg_anfr_emetteurs e
where e.departement is not null
  and e.departement not in (select departement from stg_geo_departements);

-- ----------------------------------------------------------------- dim_commune
create or replace table dim_commune as
select
    c.code_insee,
    c.nom,
    c.departement,
    d.nom as departement_nom,
    c.region_code,
    d.region,
    c.population,
    c.est_arrondissement,
    c.code_commune_parent
from stg_geo_communes c
left join stg_geo_departements d on d.departement = c.departement
union all
-- codes INSEE présents dans les données mais inconnus du référentiel (COM, anciennes communes)
select
    src.code_insee,
    any_value(src.commune),
    any_value(src.departement),
    null, null, null, null, false, null
from (
    select code_insee, commune, departement from stg_sites_indispo
    union all
    select code_insee, null, departement from stg_anfr_emetteurs
) src
where src.code_insee is not null
  and src.code_insee not in (select code_insee from stg_geo_communes)
group by src.code_insee;

-- ----------------------------------------------------------------- dim_date
-- Couvre les dates de mise en service ANFR et les relevés d'indisponibilité.
create or replace table dim_date as
with bornes as (
    select
        least((select min(date_mise_service) from stg_anfr_emetteurs),
              (select min(date_releve) from stg_sites_indispo)) as d_min,
        greatest((select max(date_mise_service) from stg_anfr_emetteurs),
                 (select max(date_releve) from stg_sites_indispo)) as d_max
)
select
    cast(d as date)                                   as date,
    year(d)                                           as annee,
    quarter(d)                                        as trimestre,
    month(d)                                          as mois,
    ['janvier', 'février', 'mars', 'avril', 'mai', 'juin', 'juillet',
     'août', 'septembre', 'octobre', 'novembre', 'décembre'][month(d)] as mois_nom,
    weekofyear(d)                                     as semaine_iso,
    isodow(d)                                         as jour_semaine,   -- 1 = lundi, 7 = dimanche
    ['lundi', 'mardi', 'mercredi', 'jeudi', 'vendredi', 'samedi', 'dimanche'][isodow(d)] as jour_nom,
    isodow(d) >= 6                                    as est_weekend
from bornes, generate_series(d_min, d_max, interval 1 day) as t(d);

-- ----------------------------------------------------------------- dim_site
-- Un site = une station ANFR (un opérateur, un emplacement).
-- On ajoute les sites connus seulement par l'Arcep (station absente de l'archive ANFR,
-- ou ligne sans numéro de station) pour que toutes les lignes de fait aient leur site.
create or replace table dim_site as
with anfr as (
    select
        station_anfr                                        as site_id,
        any_value(operateur_id)                             as operateur_id,
        mode(support_id)                                    as support_id,
        mode(code_insee)                                    as code_insee,
        mode(departement)                                   as departement,
        mode(lat)                                           as lat,
        mode(lon)                                           as lon,
        max(hauteur_m)                                      as hauteur_m,
        min(date_mise_service) filter (where est_actif)     as date_mise_service,
        count(*)                                            as nb_emetteurs,
        count(*) filter (where est_actif)                   as nb_emetteurs_actifs,
        bool_or(est_actif and techno = '2G')               as a_2g,
        bool_or(est_actif and techno = '3G')               as a_3g,
        bool_or(est_actif and techno = '4G')               as a_4g,
        bool_or(est_actif and techno = '5G')               as a_5g,
        'anfr'                                              as source
    from stg_anfr_emetteurs
    group by station_anfr
),
arcep_seul as (
    select
        site_id,
        any_value(operateur_id),
        null,
        mode(code_insee),
        mode(departement),
        mode(lat),
        mode(lon),
        null, null, 0, 0,
        bool_or(voix_2g_hs is not null),
        bool_or(voix_3g_hs is not null or data_3g_hs is not null),
        bool_or(voix_4g_hs is not null or data_4g_hs is not null),
        bool_or(data_5g_hs is not null),
        'arcep'
    from stg_sites_indispo
    where site_id not in (select site_id from anfr)
    group by site_id
)
select * from anfr
union all
select * from arcep_seul;
