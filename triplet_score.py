"""Calculate the TF-region-gene triplet ranking

The triplet ranking is the aggregated ranking of TF-to-region scores, region-to-gene scores and TF-to-region scores. 
The TF-to-gene and TF-to-region scores are defined as the feature importance scores for predicting gene expression from resp. TF expression and region accessibility.
The TF-to-region score is defined as the maximum motif-score-rank for a certain region across all motifs annotated to the TF of interest.

"""

import numba
import numpy as np
import mudata
import pandas as pd
import pyranges as pr
from scenicplus.utils import region_names_to_coordinates
from pycistarget.motif_enrichment_cistarget import cisTargetDatabase

@numba.jit(nopython=True)
def _calculate_cross_species_rank_ratio_with_order_statistics(motif_id_rank_ratios_for_one_region_or_gene: np.ndarray) -> np.ndarray:
    """
    Calculate cross-species combined rank ratio for a region/gene from rank ratios of a certain region/gene scored for
    a certain motif in multiple species with order statistics.
    Code based on applyOrderStatistics function:
      https://github.com/aertslab/orderstatistics/blob/master/OrderStatistics.java
    Paper:
      https://www.nature.com/articles/nbt1203
    :param motif_id_rank_ratios_for_one_region_or_gene:
        Numpy array of rank ratios of a certain region/gene scored for a certain motif in multiple species.
        This array is sorted inplace, so if the original array is required afterwards, provide a copy to this function.
    :return: Cross species combined rank ratio.
    FROM: https://github.com/aertslab/create_cisTarget_databases/blob/master/orderstatistics.py
    """

    # Number of species for which to calculate a cross-species combined rank ratio score.
    rank_ratios_size = motif_id_rank_ratios_for_one_region_or_gene.shape[0]

    if rank_ratios_size == 0:
        return np.float64(1.0)
    else:
        # Sort rank ratios inplace.
        motif_id_rank_ratios_for_one_region_or_gene.sort()

        w = np.zeros((rank_ratios_size + 1,), dtype=np.float64)
        w[0] = np.float64(1.0)
        w[1] = motif_id_rank_ratios_for_one_region_or_gene[rank_ratios_size - 1]

        for k in range(2, rank_ratios_size + 1):
            f = np.float64(-1.0)
            for j in range(0, k):
                f = -(f * (k - j) * motif_id_rank_ratios_for_one_region_or_gene[rank_ratios_size - k]) / (j + 1.0)
                w[k] = w[k] + (w[k - j - 1] * f)

        # Cross species combined rank ratio.
        return w[rank_ratios_size]

rng = np.random.default_rng(seed=123)
def _rank_scores_and_assign_random_ranking_in_range_for_ties(
    scores_with_ties_for_motif_or_track_numpy: np.ndarray
) -> np.ndarray:
        #
        # Create random permutation so tied scores will have a different ranking each time.
        random_permutations_to_break_ties_numpy = rng.permutation(
            scores_with_ties_for_motif_or_track_numpy.shape[0]
        )
        ranking_with_broken_ties_for_motif_or_track_numpy = random_permutations_to_break_ties_numpy[
            (-scores_with_ties_for_motif_or_track_numpy)[
                random_permutations_to_break_ties_numpy].argsort()
        ].argsort().astype(np.uint32)

        return ranking_with_broken_ties_for_motif_or_track_numpy

def get_max_rank_of_motif_for_each_TF(cistromes: mudata.AnnData, ranking_db_fname: str) -> pd.DataFrame:
    """
    Calculates the maximum rank of motifs for each transcription factor (TF) in the provided cistrome data.
    
    Args:
        cistromes: AnnData object containing cistrome data.
        ranking_db_fname: Path to the ranking database file.
    
    Returns:
        A pandas DataFrame with the maximum rank of motifs for each TF, indexed by cistrome regions.
    """
    # Read database for target regions
    pr_all_target_regions = pr.PyRanges(
        region_names_to_coordinates(cistromes.obs_names))
    ctx_db = cisTargetDatabase(
        fname=ranking_db_fname, region_sets=pr_all_target_regions)
    
    # Extract motifs
    l_motifs = [x.split(",") for x in cistromes.var["motifs"]]
    
    # Map motifs to rankings, skipping invalid entries
    l_motifs_idx = []
    to_remove = []  # To track problematic regions for removal

    for i, m in enumerate(l_motifs):
        valid_motifs = [x for x in m if x in ctx_db.db_rankings.index and x.strip()]
        if len(valid_motifs) < len(m):
            print(f"Skipping invalid motifs in region {i}: {set(m) - set(valid_motifs)}")
            # Mark region for removal if invalid motifs are present
            to_remove.append(i)
        if valid_motifs:  # Only append if there are valid motifs
            l_motifs_idx.append([ctx_db.db_rankings.index.get_loc(x) for x in valid_motifs])

    # Remove problematic regions from `cistromes` and `l_motifs`
    if to_remove:
        print(f"Removing problematic regions: {to_remove}")
        valid_indices = [i for i in range(len(l_motifs)) if i not in to_remove]
        cistromes = cistromes[:, valid_indices]
        l_motifs = [l_motifs[i] for i in valid_indices]

    rankings = ctx_db.db_rankings.to_numpy()
    
    # Generate max_rank, filling empty indices with np.inf
    max_rank = []
    for x in l_motifs_idx:
        if len(x) > 0:
            max_rank.append(rankings[x].min(0))  # Calculate min rank for valid indices
        else:
            max_rank.append(np.full(rankings.shape[1], np.inf))  # Fill with np.inf for empty motifs

    max_rank = np.array(max_rank).T
    
    
    

    
    # Convert regions to cistrome coordinates
    db_regions_cistrome_regions = ctx_db.regions_to_db.copy() \
        .groupby("Query")["Target"].apply(lambda x: list(x))
    
    print("Shape of max_rank:", max_rank.shape)
    print("Length of db_rankings columns (regions):", len(ctx_db.db_rankings.columns))
    print("Length of cistromes.var_names (TFs):", len(cistromes.var_names))
        
    
    df_max_rank = pd.DataFrame(
        max_rank,
        index=ctx_db.db_rankings.columns,  # db region names
        columns=cistromes.var_names       # TF names
    )
    df_max_rank["cistrome_region_coord"] = db_regions_cistrome_regions.loc[df_max_rank.index].values
    df_max_rank = df_max_rank.explode("cistrome_region_coord")
    df_max_rank = df_max_rank.set_index("cistrome_region_coord")
    df_max_rank = df_max_rank.groupby("cistrome_region_coord").min()
    
    print("Sample rows from df_max_rank:")
    print(df_max_rank.head())
    
    return df_max_rank



def calculate_triplet_score(
        cistromes: mudata.AnnData,
        eRegulon_metadata: pd.DataFrame,
        ranking_db_fname: str) -> pd.DataFrame:
        eRegulon_metadata = eRegulon_metadata.copy()
        df_TF_region_max_rank = get_max_rank_of_motif_for_each_TF(
              cistromes=cistromes,
              ranking_db_fname=ranking_db_fname)
        TF_region_iter = eRegulon_metadata[["TF", "Region"]].to_numpy()

        
        TF_to_region_score = []
        for TF, region in TF_region_iter:
            if TF in df_TF_region_max_rank.columns and region in df_TF_region_max_rank.index:
                TF_to_region_score.append(df_TF_region_max_rank.loc[region, TF])
            else:
                TF_to_region_score.append(0)  # Or handle appropriately
                
        TF_to_region_score = np.array(TF_to_region_score)
        TF_to_gene_score = eRegulon_metadata["importance_TF2G"].to_numpy()
        region_to_gene_score = eRegulon_metadata["importance_R2G"].to_numpy()
        #rank the scores
        TF_to_region_rank = _rank_scores_and_assign_random_ranking_in_range_for_ties(
              -TF_to_region_score) #negate because lower score is better
        TF_to_gene_rank = _rank_scores_and_assign_random_ranking_in_range_for_ties(
              TF_to_gene_score)
        region_to_gene_rank = _rank_scores_and_assign_random_ranking_in_range_for_ties(
              region_to_gene_score)
        #create rank ratios
        TF_to_gene_rank_ratio = (TF_to_gene_rank.astype(np.float64) + 1) / TF_to_gene_rank.shape[0]
        region_to_gene_rank_ratio = (region_to_gene_rank.astype(np.float64) + 1) / region_to_gene_rank.shape[0]
        TF_to_region_rank_ratio = (TF_to_region_rank.astype(np.float64) + 1) / TF_to_region_rank.shape[0]

        #create aggregated rank
        rank_ratios = np.array([
              TF_to_gene_rank_ratio, region_to_gene_rank_ratio, TF_to_region_rank_ratio])
        aggregated_rank = np.zeros((rank_ratios.shape[1],), dtype = np.float64)
        for i in range(rank_ratios.shape[1]):
                aggregated_rank[i] = _calculate_cross_species_rank_ratio_with_order_statistics(rank_ratios[:, i])
        eRegulon_metadata["triplet_rank"] = aggregated_rank.argsort().argsort()
        return eRegulon_metadata