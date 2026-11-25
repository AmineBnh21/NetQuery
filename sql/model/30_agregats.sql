-- Agrégats : répondent aux questions courantes sans scanner les faits détaillés.

-- ----------------------------------------------------------------- agg_indispo_dept_jour
-- Grain : jour x département x opérateur x service x techno.
-- Non additif entre technos (un site peut être HS en 3G et en 4G).
create or replace table agg_indispo_dept_jour as
select
    date,
    departement,
    operateur_id,
    service,
    techno,
    count(distinct site_id) filter (where est_hs) as nb_sites_hs,
    count(distinct site_id)                       as nb_sites_declares
from fact_indispo
group by all;

-- ----------------------------------------------------------------- agg_sites_hs_dept_jour
-- Grain : jour x département x opérateur, toutes technos confondues (additif entre départements).
create or replace table agg_sites_hs_dept_jour as
select
    date,
    departement,
    operateur_id,
    count(*)                                        as nb_sites_indispo,
    count(*) filter (where voix_hs)                 as nb_sites_voix_hs,
    count(*) filter (where data_hs)                 as nb_sites_data_hs,
    count(*) filter (where voix_hs and data_hs)     as nb_sites_voix_et_data_hs,
    count(*) filter (where raison = 'incident')     as nb_sites_incident,
    count(*) filter (where raison = 'maintenance')  as nb_sites_maintenance
from fact_indispo_site
group by all;
