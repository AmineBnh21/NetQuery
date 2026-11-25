-- Tables de faits.

-- ----------------------------------------------------------------- fact_station
-- Grain : un émetteur ANFR (site x système radio).
create or replace table fact_station as
select
    emetteur_id,
    station_anfr       as site_id,
    operateur_id,
    departement,
    code_insee,
    techno,
    systeme,
    norme,
    bande_mhz,
    statut,
    est_actif,
    date_mise_service
from stg_anfr_emetteurs;

-- ----------------------------------------------------------------- fact_indispo_site
-- Grain : un site hors service (au moins partiellement) un jour donné.
-- A utiliser pour compter des sites : une ligne = un site.
create or replace table fact_indispo_site as
select
    date_releve                               as date,
    site_id,
    operateur_id,
    departement,
    code_insee,
    raison,
    detail,
    propre,
    voix_hs,
    data_hs,
    debut,
    fin_prevue,
    debut_voix,
    fin_voix_prevue,
    debut_data,
    fin_data_prevue,
    -- ancienneté de la panne au jour du relevé
    date_diff('day', cast(debut as date), date_releve) as nb_jours_depuis_debut
from stg_sites_indispo;

-- ----------------------------------------------------------------- fact_indispo
-- Format long. Grain : site x jour x service x technologie.
-- Seules les technos présentes sur le site ont une ligne (est_hs non null).
-- Attention : un site HS en 3G et en 4G compte 2 lignes ; pour compter des sites
-- toutes technos confondues, utiliser count(distinct site_id) ou fact_indispo_site.
create or replace table fact_indispo as
with longue as (
    unpivot stg_sites_indispo
    on voix_2g_hs, voix_3g_hs, voix_4g_hs, data_3g_hs, data_4g_hs, data_5g_hs
    into name colonne value est_hs
)
select
    date_releve                                 as date,
    site_id,
    operateur_id,
    departement,
    code_insee,
    split_part(colonne, '_', 1)                 as service,   -- voix, data
    upper(split_part(colonne, '_', 2))          as techno,    -- 2G, 3G, 4G, 5G
    est_hs,
    raison
from longue;  -- unpivot exclut déjà les valeurs nulles
