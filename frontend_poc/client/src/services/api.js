import axios from "axios";

const API_URL = "http://localhost:3000/api";

// Create axios instance with base URL
const api = axios.create({
  baseURL: API_URL,
  headers: {
    "Content-Type": "application/json",
  },
});

// Helper function for file uploads (uses different content type)
const fileApi = axios.create({
  baseURL: API_URL,
  headers: {
    "Content-Type": "multipart/form-data",
  },
});

// Contract upload and fetching
export const uploadContract = (formData) => {
  return fileApi.post("/upload-contract", formData);
};

export const fetchContractByAddress = (data) => {
  return api.post("/fetch-contract", data);
};

// Analysis
export const startAnalysis = (data) => {
  return api.post("/analyze", data);
};

// Status and results
export const fetchContractStatus = (jobId) => {
  return api.get(`/status/${jobId}`);
};

export const fetchContractResults = (jobId) => {
  return api.get(`/results/${jobId}`);
};

export const startDemoJob = () => {
  return api.post("/demo");
};

export const startDemoRealJob = () => {
  // Hardcoded address and network for MultiContract demo
  return api.post("/fetch-contract", {
    address: "0xMULTICONTRACTDEMOADDRESS", // Replace with actual address if needed
    network: "ethereum", // Or the correct network
    saveSeparate: true
  });
};

export const startDemoLocalMultiContractJob = () => {
  return api.post("/demo-local-multicontract");
};

export default api;
