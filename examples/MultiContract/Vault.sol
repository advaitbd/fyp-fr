// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

import "./DEX.sol";

contract Vault {
    DEX public dex;
    mapping(address => uint256) public deposits;

    constructor(address _dex) {
        dex = DEX(_dex);
    }

    function deposit() public payable {
        require(msg.value > 0, "Send ETH to deposit");
        deposits[msg.sender] += msg.value;
    }

    // Vulnerability: Relies on DEX price, which can be manipulated via Oracle
    function withdrawAll() public {
        uint256 userDeposit = deposits[msg.sender];
        require(userDeposit > 0, "Nothing to withdraw");
        // Get price from DEX (which gets it from Oracle)
        uint256 price = dex.oracle().getPrice();
        // Attacker can manipulate price to withdraw more than deposited
        uint256 payout = userDeposit * price;
        deposits[msg.sender] = 0;
        payable(msg.sender).transfer(payout);
    }

    // Helper to fund the vault for demo
    receive() external payable {}
} 