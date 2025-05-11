// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

import "./Oracle.sol";

contract DEX {
    Oracle public oracle;
    mapping(address => uint256) public balances;

    constructor(address _oracle) {
        oracle = Oracle(_oracle);
    }

    // Vulnerability: Uses manipulatable oracle price
    function buy() public payable {
        require(msg.value > 0, "Send ETH to buy tokens");
        uint256 price = oracle.getPrice();
        require(price > 0, "Invalid price");
        uint256 tokens = msg.value * price;
        balances[msg.sender] += tokens;
    }

    function sell(uint256 tokens) public {
        require(balances[msg.sender] >= tokens, "Not enough tokens");
        uint256 price = oracle.getPrice();
        uint256 ethAmount = tokens / price;
        balances[msg.sender] -= tokens;
        payable(msg.sender).transfer(ethAmount);
    }
} 