#include "memory_pool.h"

#include "compartment.h"
#include "pointer.h"

#include <util/system/align.h>

namespace NYT::NWebAssembly {

////////////////////////////////////////////////////////////////////////////////

TWebAssemblyMemoryPool::TWebAssemblyMemoryPool()
    : Compartment_(GetCurrentCompartment())
{ }

TWebAssemblyMemoryPool::TWebAssemblyMemoryPool(IWebAssemblyCompartment* compartment)
    : Compartment_(compartment)
{ }

TWebAssemblyMemoryPool::~TWebAssemblyMemoryPool()
{
    Clear();
}

TWebAssemblyMemoryPool::TWebAssemblyMemoryPool(TWebAssemblyMemoryPool&& other)
{
    std::swap(Size_, other.Size_);
    Allocations_.swap(other.Allocations_);
}

TWebAssemblyMemoryPool& TWebAssemblyMemoryPool::operator=(TWebAssemblyMemoryPool&& other)
{
    std::swap(Size_, other.Size_);
    Allocations_.swap(other.Allocations_);
    return *this;
}

void TWebAssemblyMemoryPool::Clear()
{
    for (const auto& address : Allocations_) {
        Compartment_->FreeBytes(address);
    }
    Allocations_.clear();
    Size_ = 0;
}

size_t TWebAssemblyMemoryPool::GetSize() const
{
    return Size_;
}

size_t TWebAssemblyMemoryPool::GetCapacity() const
{
    return Size_;
}

char* TWebAssemblyMemoryPool::AllocateUnaligned(size_t size)
{
    auto offset = Compartment_->AllocateBytes(size);
    Allocations_.push_back(offset);
    Size_ += size;
    return std::bit_cast<char*>(offset);
}

char* TWebAssemblyMemoryPool::AllocateAligned(size_t size, int align)
{
    auto unaligned = Compartment_->AllocateBytes(size + align);
    Allocations_.push_back(unaligned);
    auto aligned = AlignUp(std::bit_cast<char*>(unaligned), align);
    return std::bit_cast<char*>(aligned);
}

////////////////////////////////////////////////////////////////////////////////

} // namespace NYT::NWebAssembly
