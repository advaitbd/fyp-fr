import React, { useState, useRef } from "react";
import { uploadContract, fetchContractByAddress } from "../services/api";

const ContractInput = ({ onContractSubmit }) => {
  const [isUploading, setIsUploading] = useState(false);
  const [isFetching, setIsFetching] = useState(false);
  const [uploadError, setUploadError] = useState(null);
  const [fetchError, setFetchError] = useState(null);
  const [contractAddress, setContractAddress] = useState("");
  const [network, setNetwork] = useState("ethereum");
  const [saveSeparate, setSaveSeparate] = useState(true);
  const fileInputRef = useRef(null);
  const dragAreaRef = useRef(null);

  const handleDragOver = (e) => {
    e.preventDefault();
    dragAreaRef.current.classList.add("border-blue-500");
  };

  const handleDragLeave = () => {
    dragAreaRef.current.classList.remove("border-blue-500");
  };

  const handleDrop = (e) => {
    e.preventDefault();
    dragAreaRef.current.classList.remove("border-blue-500");

    if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
      handleFileUpload(e.dataTransfer.files[0]);
    }
  };

  const handleFileChange = (e) => {
    if (e.target.files && e.target.files.length > 0) {
      handleFileUpload(e.target.files[0]);
    }
  };

  const handleFileUpload = async (file) => {
    // Check if file is a Solidity file
    if (!file.name.endsWith(".sol")) {
      setUploadError("Only Solidity (.sol) files are supported");
      return;
    }

    setIsUploading(true);
    setUploadError(null);

    try {
      const formData = new FormData();
      formData.append("file", file);

      const response = await uploadContract(formData);
      onContractSubmit({
        id: response.data.job_id,
        name: file.name,
        status: response.data.status,
      });
    } catch (error) {
      setUploadError(error.response?.data?.error || "Error uploading contract");
    } finally {
      setIsUploading(false);
    }
  };

  const handleAddressFetch = async (e) => {
    e.preventDefault();

    if (!contractAddress.trim()) {
      setFetchError("Contract address is required");
      return;
    }

    setIsFetching(true);
    setFetchError(null);

    try {
      console.log("Fetching contract:", contractAddress, network);
      const response = await fetchContractByAddress({
        address: contractAddress,
        network: network,
        saveSeparate: saveSeparate,
      });

      console.log("Contract fetch response:", response.data);

      onContractSubmit({
        id: response.data.job_id,
        name: `${contractAddress}.sol`,
        status: response.data.status,
      });
    } catch (error) {
      console.error("Error fetching contract:", error);
      setFetchError(error.response?.data?.error || "Error fetching contract");
    } finally {
      setIsFetching(false);
    }
  };

  return (
    <div className="bg-white p-6 rounded-lg shadow-md">
      <h2 className="text-xl font-semibold mb-4">Contract Input</h2>

      {/* File Upload Area */}
      <div className="mb-6">
        <h3 className="text-lg font-medium mb-2">Upload Solidity File</h3>
        <div
          ref={dragAreaRef}
          className="border-2 border-dashed border-gray-300 rounded-lg p-6 text-center cursor-pointer transition-colors duration-200"
          onClick={() => fileInputRef.current.click()}
          onDragOver={handleDragOver}
          onDragLeave={handleDragLeave}
          onDrop={handleDrop}
        >
          <input
            type="file"
            ref={fileInputRef}
            className="hidden"
            accept=".sol"
            onChange={handleFileChange}
          />
          {/* <svg className="mx-auto h-12 w-12 text-gray-400" stroke="currentColor" fill="none" viewBox="0 0 48 48">
            <path d="M28 8H12a4 4 0 00-4 4v20m32-12v8m0 0v8a4 4 0 01-4 4h-8m-12 0H8m12 0a4 4 0 01-4-4v-4m32 0v-4a4 4 0 00-4-4h-4m-12 0h-4a4 4 0 00-4 4v4m4-4h32" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
          </svg> */}
          <p className="mt-1 text-sm text-gray-600">
            Drag and drop a .sol file here, or click to select file
          </p>
          {isUploading && (
            <p className="mt-2 text-sm text-blue-500">Uploading...</p>
          )}
          {uploadError && (
            <p className="mt-2 text-sm text-red-500">{uploadError}</p>
          )}
        </div>
      </div>

      {/* Contract Address Input */}
      <div>
        <h3 className="text-lg font-medium mb-2">Fetch by Contract Address</h3>
        <form onSubmit={handleAddressFetch} className="space-y-3">
          <div>
            <label
              htmlFor="network"
              className="block text-sm font-medium text-gray-700"
            >
              Network
            </label>
            <select
              id="network"
              className="mt-1 block w-full rounded-md border-gray-300 py-2 px-3 shadow-sm focus:border-blue-500 focus:ring-blue-500"
              value={network}
              onChange={(e) => setNetwork(e.target.value)}
            >
              <option value="ethereum">Ethereum</option>
              <option value="bsc">Binance Smart Chain</option>
              <option value="base">Base</option>
              <option value="arbitrum">Arbitrum</option>
            </select>
          </div>

          <div>
            <label
              htmlFor="address"
              className="block text-sm font-medium text-gray-700"
            >
              Contract Address
            </label>
            <input
              type="text"
              id="address"
              placeholder="0x..."
              className="mt-1 block w-full rounded-md border-gray-300 py-2 px-3 shadow-sm focus:border-blue-500 focus:ring-blue-500"
              value={contractAddress}
              onChange={(e) => setContractAddress(e.target.value)}
            />
          </div>

          <div className="mt-3">
            <label className="inline-flex items-center cursor-pointer">
              <input
                type="checkbox"
                className="sr-only peer"
                checked={saveSeparate}
                onChange={(e) => setSaveSeparate(e.target.checked)}
              />
              <div className="relative w-11 h-6 bg-gray-200 peer-focus:outline-none peer-focus:ring-2 peer-focus:ring-blue-300 rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:border-gray-300 after:border after:rounded-full after:h-5 after:w-5 after:transition-all peer-checked:bg-blue-600"></div>
              <span className="ml-3 text-sm font-medium text-gray-700">
                Enable Project Context Agent
              </span>
            </label>
            <p className="text-xs text-gray-500 mt-1">
              Preserves project structure for inter-contract relationship
              analysis
            </p>
          </div>

          <button
            type="submit"
            disabled={isFetching}
            className="w-full py-2.5 px-5 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-gradient-to-r from-blue-600 to-indigo-600 hover:from-blue-700 hover:to-indigo-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-blue-500 disabled:opacity-50 disabled:cursor-not-allowed transition-all duration-200"
          >
            {isFetching ? "Fetching..." : "Fetch Contract"}
          </button>

          {fetchError && (
            <p className="mt-2 text-sm text-red-500">{fetchError}</p>
          )}
        </form>
      </div>
    </div>
  );
};

export default ContractInput;
