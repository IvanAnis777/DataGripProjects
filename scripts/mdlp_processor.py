#!/usr/bin/env python3
"""
CLI script for processing MDLP data.

Converted from notebook MDLP.ipynb for use in production.
"""

import argparse
import logging
import sys
import os
from pathlib import Path
from typing import Optional, List

import pandas as pd

# Add path to helpers module
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helpers import (
    get_db_connection, 
    search_gtin_in_database, 
    save_dataframe_safe,
    validate_dataframe,
    DatabaseConfig,
    logger,
    normalize_gtin
)

def process_gtin_file(input_file, gtin_column='GTIN'):
    """
    Process GTIN data from file
    
    Args:
        input_file: Path to input file
        gtin_column: Name of GTIN column
        
    Returns:
        DataFrame: Processed data with search results
    """
    logger = logging.getLogger(__name__)
    
    # Load data
    logger.info(f"Loading data from {input_file}")
    if input_file.endswith('.xlsx'):
        df = pd.read_excel(input_file)
    else:
        df = pd.read_csv(input_file)
    
    logger.info(f"Loaded {len(df)} records")
    
    if gtin_column not in df.columns:
        raise ValueError(f"Column '{gtin_column}' not found in data")
    
    # Normalize GTINs
    logger.info("Normalizing GTIN codes...")
    df['gtin_normalized'] = df[gtin_column].apply(normalize_gtin)
    
    # Search GTINs in database
    logger.info("Searching GTINs in database...")
    gtin_list = df['gtin_normalized'].dropna().unique().tolist()
    found_gtins, missing_gtins = search_gtin_in_database(gtin_list)
    
    # Add search results
    df['found_in_db'] = df['gtin_normalized'].isin(found_gtins)
    
    # Statistics
    found_count = df['found_in_db'].sum()
    total_count = len(df)
    
    logger.info(f"Found in database: {found_count}/{total_count} ({found_count/total_count*100:.1f}%)")
    
    return df

def main():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    logger = logging.getLogger(__name__)
    
    parser = argparse.ArgumentParser(description='Process MDLP/GTIN data')
    parser.add_argument('input_file', help='Input CSV/Excel file with GTIN data')
    parser.add_argument('-o', '--output', help='Output file (default: add _processed suffix)')
    parser.add_argument('-c', '--gtin-column', default='GTIN',
                       help='Name of GTIN column (default: GTIN)')
    parser.add_argument('--missing-only', action='store_true',
                       help='Save only missing GTINs')
    parser.add_argument('--found-only', action='store_true',
                       help='Save only found GTINs')
    parser.add_argument('--verbose', action='store_true', help='Detailed logging')
    
    args = parser.parse_args()
    
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    try:
        # Determine output file
        if args.output:
            output_file = args.output
        else:
            base_name = os.path.splitext(args.input_file)[0]
            ext = os.path.splitext(args.input_file)[1]
            output_file = f"{base_name}_processed{ext}"
        
        # Process data
        result_df = process_gtin_file(args.input_file, args.gtin_column)
        
        # Apply filters
        original_count = len(result_df)
        if args.missing_only:
            result_df = result_df[~result_df['found_in_db']]
            logger.info(f"Filtered to {len(result_df)} missing GTINs")
        elif args.found_only:
            result_df = result_df[result_df['found_in_db']]
            logger.info(f"Filtered to {len(result_df)} found GTINs")
        
        # Save result
        if save_dataframe_safe(result_df, output_file):
            logger.info(f"Results saved to {output_file}")
            logger.info(f"Processed {original_count} records -> {len(result_df)} saved")
        else:
            logger.error("Failed to save results")
            return 1
        
        return 0
        
    except Exception as e:
        logger.error(f"Error: {e}")
        return 1

if __name__ == '__main__':
    exit(main()) 