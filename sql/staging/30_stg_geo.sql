-- Référentiel géographique (geo.api.gouv.fr), typé.

create or replace table stg_geo_communes as
select
    code                            as code_insee,
    nom,
    codeDepartement                 as departement,
    codeRegion                      as region_code,
    try_cast(population as integer) as population,
    false                           as est_arrondissement,
    cast(null as varchar)           as code_commune_parent
from raw_geo_communes
union all
-- arrondissements municipaux de Paris, Lyon, Marseille (codes utilisés par l'ANFR et l'Arcep)
select
    code, nom, codeDepartement, codeRegion, try_cast(population as integer), true, codeParent
from raw_geo_arrondissements;

create or replace table stg_geo_departements as
select d.code as departement, d.nom, d.codeRegion as region_code, r.nom as region
from raw_geo_departements d
left join raw_geo_regions r on r.code = d.codeRegion;
