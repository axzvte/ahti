#include "circuit_ir.hpp"
#include <opencv2/imgcodecs.hpp>
#include <filesystem>
#include <iostream>

namespace {
int integer(const char* value) {
    std::size_t end = 0;
    const std::string text(value);
    const int parsed = std::stoi(text, &end);
    if (end != text.size()) {
        throw std::runtime_error("Invalid integer: " + text);
    }
    return parsed;
}

void save(const std::filesystem::path& directory, const char* name, const cv::Mat& image) {
    if (!cv::imwrite((directory / name).string(), image)) {
        throw std::runtime_error(std::string("Cannot write ") + name);
    }
}

}
int main(int argc, char** argv) {
    try {
        if (argc == 2 && std::string(argv[1]) == "--capabilities") {
            std::cout << "{\"circuit_ir\":1,\"approximate_replacements\":1}\n";
            return 0;
        }
        if (argc < 12 || std::string(argv[1]) != "ir" || std::string(argv[argc-2]) != "--circuit")
            throw std::runtime_error("Usage: fixed_roi_verify ir IMAGE OUT LEFT TOP RIGHT BOTTOM SITE BIT [GROUP or GROUP=UNIT ...] --circuit PROFILE");
        const std::string profilePath(argv[argc-1]);
        argc -= 2;
        const auto image=cv::imread(argv[2], cv::IMREAD_GRAYSCALE | cv::IMREAD_IGNORE_ORIENTATION);
        if (image.empty()) throw std::runtime_error("Cannot read input image.");
        const int left=integer(argv[4]), top=integer(argv[5]);
        const int right=integer(argv[6]), bottom=integer(argv[7]);
        if (left<0 || top<0 || left>=right || top>=bottom || right>image.cols || bottom>image.rows)
            throw std::runtime_error("Invalid nonempty half-open ROI.");
        const cv::Rect region(left,top,right-left,bottom-top);
        std::filesystem::create_directories(argv[3]);
            const auto ir=circuit_ir::Profile::load(profilePath);
            const std::string site(argv[8]); const int bit=integer(argv[9]);
            std::vector<int> replacements;
            std::map<int,circuit_profile::BaselineNode> approximateReplacements;
            for (int i=10; i<argc; ++i) {
                const std::string token(argv[i]);
                const auto separator=token.find('=');
                if (separator==std::string::npos) {
                    replacements.push_back(integer(argv[i]));
                } else {
                    const int group=integer(token.substr(0,separator).c_str());
                    const auto unit=circuit_profile::unit(token.substr(separator+1));
                    circuit_ir::need(!unit.exact && approximateReplacements.emplace(group,unit).second,
                                     "expected a unique approximate replacement group and unit");
                }
            }
            auto compensated=ir.compensatedConfig(replacements);
            for (const auto& [group,unit]:approximateReplacements) {
                const auto found=compensated.find(group);
                circuit_ir::need(found!=compensated.end() && !found->second.exact,
                                 "approximate replacement must name an original approximate group without exact replacement");
                found->second=unit;
            }
            const auto exact=ir.run(image,ir.exactConfig());
            const auto baseline=ir.run(image,ir.baseline);
            const auto noFlip=ir.run(image,compensated);
            cv::Mat processed;
            processed=ir.run(image,compensated,region,site,bit);
            save(argv[3],"exact.png",exact); save(argv[3],"baseline.png",baseline);
            save(argv[3],"compensated.png",noFlip); save(argv[3],"processed.png",processed);
        return 0;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
