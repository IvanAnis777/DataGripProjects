#!/usr/bin/env python3
"""
CLI script for cleaning and normalizing addresses.

Converted from notebook adress clearn.ipynb for use in production.
"""

import argparse
import logging
import sys
import os
from pathlib import Path
from typing import Optional

import pandas as pd

# Add path to helpers module
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helpers import (
    clean_address,
    check_broken_address,
    save_dataframe_safe,
    validate_dataframe,
    logger
)


def process_addresses(df: pd.DataFrame, 
                     address_column: str = 'address',
                     add_validation: bool = True) -> pd.DataFrame:
    """
    Process addresses in DataFrame
    
    Args:
        df: Input DataFrame
        address_column: Name of address column
        add_validation: Add validation column
        
    Returns:
        DataFrame: Processed DataFrame with cleaned addresses
    """
    logger = logging.getLogger(__name__)
    
    # Create copy of DataFrame
    result_df = df.copy()
    
    if address_column not in df.columns:
        raise ValueError(f"Column '{address_column}' not found in data")
    
    logger.info(f"Processing {len(df)} addresses...")
    
    # Address cleaning
    result_df['address_cleaned'] = result_df[address_column].apply(clean_address)
    
    # Add validation
    if add_validation:
        result_df['is_valid_address'] = ~result_df['address_cleaned'].apply(check_broken_address)
        
        # Validation statistics
        valid_count = result_df['is_valid_address'].sum()
        total_count = len(result_df)
        
        logger.info(f"Valid addresses: {valid_count}/{total_count} ({valid_count/total_count*100:.1f}%)")
    
    return result_df


def filter_addresses(df: pd.DataFrame, 
                    filter_type: str = 'valid') -> pd.DataFrame:
    """
    Filter addresses by type.
    
    Args:
        df: DataFrame with 'is_valid_address' column
        filter_type: Filter type ('valid', 'invalid', 'all')
        
    Returns:
        DataFrame: Filtered DataFrame
    """
    if filter_type == 'all':
        return df
    
    if 'is_valid_address' not in df.columns:
        logger.warning("No 'is_valid_address' column found, returning all records")
        return df
    
    if filter_type == 'valid':
        filtered_df = df[df['is_valid_address']].copy()
        logger.info(f"Filtered to {len(filtered_df)} valid addresses")
    elif filter_type == 'invalid':
        filtered_df = df[~df['is_valid_address']].copy()
        logger.info(f"Filtered to {len(filtered_df)} invalid addresses")
    else:
        raise ValueError(f"Invalid filter_type: {filter_type}. Use 'valid', 'invalid', or 'all'")
    
    return filtered_df


def generate_address_report(df: pd.DataFrame) -> dict:
    """
    Generate address processing report
    
    Args:
        df: DataFrame with processed addresses
        
    Returns:
        dict: Report with statistics
    """
    report = {
        'total_records': len(df),
        'has_address_cleaned': 'address_cleaned' in df.columns,
        'has_validation': 'is_valid_address' in df.columns
    }
    
    if report['has_validation']:
        report['valid_addresses'] = df['is_valid_address'].sum()
        report['invalid_addresses'] = (~df['is_valid_address']).sum()
        report['validity_rate'] = report['valid_addresses'] / report['total_records'] * 100
    
    # Address length analysis
    if 'address_cleaned' in df.columns:
        addr_lengths = df['address_cleaned'].str.len()
        report['avg_address_length'] = addr_lengths.mean()
        report['short_addresses'] = addr_lengths.lt(10).sum()
        report['long_addresses'] = addr_lengths.gt(100).sum()
    
    return report


def main():
    """Main function for CLI."""
    # Logging setup
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    logger = logging.getLogger(__name__)
    
    parser = argparse.ArgumentParser(description='Clean and normalize Russian addresses')
    parser.add_argument('input_file', help='Input CSV/Excel file with addresses')
    parser.add_argument('-o', '--output', help='Output file (default: add _cleaned suffix)')
    parser.add_argument('-c', '--address-column', default='address', 
                       help='Name of address column (default: address)')
    parser.add_argument('--filter', choices=['valid', 'invalid', 'all'], default='all',
                       help='Filter results (default: all)')
    parser.add_argument('--report', action='store_true', 
                       help='Generate processing report')
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
            output_file = f"{base_name}_cleaned{ext}"
        
        # Load data
        logger.info(f"Loading data from {args.input_file}")
        if args.input_file.endswith('.xlsx'):
            df = pd.read_excel(args.input_file)
        else:
            df = pd.read_csv(args.input_file)
        
        # Validate input data
        if args.address_column not in df.columns:
            logger.error(f"Column '{args.address_column}' not found")
            logger.info(f"Available columns: {list(df.columns)}")
            return 1
        
        # Process addresses
        result_df = process_addresses(df, args.address_column)
        
        # Filtering
        if args.filter == 'valid':
            result_df = result_df[result_df['is_valid_address']]
            logger.info(f"Filtered to {len(result_df)} valid addresses")
        elif args.filter == 'invalid':
            result_df = result_df[~result_df['is_valid_address']]
            logger.info(f"Filtered to {len(result_df)} invalid addresses")
        
        # Save result
        if save_dataframe_safe(result_df, output_file):
            logger.info(f"Results saved to {output_file}")
        else:
            logger.error(f"Failed to save results")
            return 1
        
        # Generate report
        if args.report:
            report = generate_address_report(result_df)
            
            print(f"\n=== ADDRESS CLEANING REPORT ===")
            print(f"Total records: {report['total_records']}")
            if report['has_validation']:
                print(f"Valid addresses: {report['valid_addresses']}")
                print(f"Invalid addresses: {report['invalid_addresses']}")
                print(f"Validity rate: {report['validity_rate']:.1f}%")
            if 'avg_address_length' in report:
                print(f"Average address length: {report['avg_address_length']:.1f} characters")
                print(f"Short addresses (<10 characters): {report['short_addresses']}")
                print(f"Long addresses (>100 characters): {report['long_addresses']}")
            print(f"Output records: {len(result_df)}")
            print(f"Output file: {output_file}")
        
        return 0
        
    except Exception as e:
        logger.error(f"Error: {e}")
        return 1


if __name__ == '__main__':
    exit(main()) 