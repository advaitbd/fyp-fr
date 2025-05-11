// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

contract Oracle {
    uint256 public price;

    // Vulnerability: Anyone can set the price!
    function setPrice(uint256 _price) public {
        price = _price;
    }

    function getPrice() public view returns (uint256) {
        return price;
    }
} 