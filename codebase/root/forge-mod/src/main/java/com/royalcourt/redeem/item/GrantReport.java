package com.royalcourt.redeem.item;

import java.util.List;

/**
 * What one redemption actually granted, and which keys could not be mapped to
 * an item on this server (reported to the player so staff can compensate).
 */
public record GrantReport(List<String> granted, List<String> missing) {
}
